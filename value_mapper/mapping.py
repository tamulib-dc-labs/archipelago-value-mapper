"""
Spreadsheet-driven cleanup of name, subject and genre values in Archipelago.

The round trip:

1. ``get_metadata`` reads every ADO through the site's metadata endpoint and
   writes a workbook with one sheet per metadata key and one row per distinct
   value, with empty *target* columns.
2. Someone fills in targets for the values that need fixing.
3. ``update_metadata`` finds the ADOs holding a source value through the same
   endpoint, then rewrites each of them to its target through JSON:API.

The rules for a row:

* a row with every target cell blank is ignored
* a filled target cell replaces only that part, so filling just ``target_uri``
  fixes the URI and keeps the label
* a value is matched exactly after trimming whitespace -- case-sensitive on
  purpose, so "Texas" and "texas" arrive as two rows to reconcile
* for ``agent`` and ``agent_linked_data`` the role is part of the match, so the
  same name with two roles is two rows
* when two sources map to the same target on one ADO, the duplicate is dropped
* a key stored as a single value rather than a list is written back the same way
* any other keys on an entry (beyond value/uri/role) are carried through
"""

import csv
import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import xlsxwriter
from python_calamine import CalamineWorkbook

from .client import Ado, ArchipelagoClient, Instance

T = TypeVar("T")

# How a long loop reports progress: given the items and a label, return them
# (possibly wrapped). The CLI passes a Rich progress bar; the default is silent.
Track = Callable[[Iterable[T], str], Iterable[T]]


def no_progress(items: Iterable[T], _label: str) -> Iterable[T]:
    return items


@dataclass(frozen=True)
class KeySpec:
    # plain: the key holds strings. parts: the entry keys a row matches on and
    # can change, in column order.
    plain: bool
    parts: tuple[str, ...]


PLAIN = KeySpec(True, ("value",))
LINKED = KeySpec(False, ("value", "uri"))

# Shapes from archipelago-metadata-mappings/mappings/main.yml. Order here is
# sheet order in the workbook. drupal/tamu_metadata_api lists the same keys;
# tests/test_keys_in_sync.py keeps the two in step.
KEYS: dict[str, KeySpec] = {
    "creator": PLAIN,
    "creator_linked_data": LINKED,
    "contributor": PLAIN,
    "contributor_linked_data": LINKED,
    "agent": KeySpec(False, ("value", "role")),
    "agent_linked_data": KeySpec(False, ("value", "uri", "role")),
    "subject": PLAIN,
    "subject_linked_data": LINKED,
    "geographic_subject": PLAIN,
    "geographic_subject_linked_data": LINKED,
    "temporal_subject": PLAIN,
    "temporal_subject_linked_data": LINKED,
    "genre_form": PLAIN,
    "genre_form_linked_data": LINKED,
}

COUNT_COLUMNS = ["ado_count", "example_nid"]
INFO_SHEET = "_info"

REPORT_COLUMNS = ["nid", "uuid", "title", "key", "old value", "new value", "status"]

# Control characters an .xlsx cannot hold.
ILLEGAL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# {key: {source identity: {part: new value}}}
Mapping = dict[str, dict[tuple[str, ...], dict[str, str]]]


class MappingWorkbookError(ValueError):
    """A workbook the operator has to fix before anything is written."""


@dataclass
class Change:
    key: str
    old: str
    new: str


@dataclass
class Candidate:
    ado: Ado
    changes: list[Change]


@dataclass
class GetResult:
    path: Path
    total: int
    distinct: dict[str, int]


@dataclass
class UpdateResult:
    records: list[dict[str, Any]]
    candidates: int
    updated: int
    dry_run: bool
    report_path: Path | None = None
    errors: int = field(init=False)

    def __post_init__(self):
        self.errors = sum(1 for record in self.records if record["status"].startswith("error"))


def source_columns(spec: KeySpec) -> list[str]:
    return ["source"] if spec.plain else [f"source_{part}" for part in spec.parts]


def target_columns(spec: KeySpec) -> list[str]:
    return ["target"] if spec.plain else [f"target_{part}" for part in spec.parts]


def clean(value: Any) -> str:
    """
    Trim, and drop the control characters Excel cannot store.

    Applied the same way on the way out and on the way back in, so a value
    carrying one still matches its own row. A whole number Excel hands back
    as 1920.0 becomes "1920".
    """
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return ILLEGAL_CHARACTERS.sub("", str(value)).strip()


def as_entries(stored: Any) -> tuple[list, bool]:
    """
    A key's stored value as (list of entries, was a single value).

    Archipelago tolerates a bare string or object where the profile says
    array, so both shapes turn up and both are handled.
    """
    if stored is None or stored == "" or stored == []:
        return [], False
    if isinstance(stored, list):
        return [entry for entry in stored if entry not in (None, "")], False
    return [stored], True


def identity(entry: Any, spec: KeySpec) -> tuple[str, ...]:
    """The tuple a row matches on. A bare string in an object key is its value."""
    if isinstance(entry, dict):
        return tuple(clean(entry.get(part)) for part in spec.parts)
    if spec.plain:
        return (clean(entry),)
    return (clean(entry),) + ("",) * (len(spec.parts) - 1)


def display(entry: Any, spec: KeySpec) -> str:
    if not isinstance(entry, dict):
        return clean(entry)
    value, *rest = identity(entry, spec)
    extras = dict(zip(spec.parts[1:], rest, strict=True))
    if extras.get("uri"):
        value += f" <{extras['uri']}>"
    if extras.get("role"):
        value += f" ({extras['role']})"
    return value


def _apply_rule(entry: Any, spec: KeySpec, rule: dict[str, str]) -> Any:
    if spec.plain and not isinstance(entry, dict):
        return rule["value"]
    updated = dict(entry) if isinstance(entry, dict) else {"value": entry}
    updated.update(rule)
    return updated


def _dedupe(entries: list) -> list:
    seen, kept = set(), []
    for entry in entries:
        marker = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        if marker not in seen:
            seen.add(marker)
            kept.append(entry)
    return kept


def apply_mapping(metadata: dict, mapping: Mapping, keys: dict[str, KeySpec] = KEYS) -> tuple[dict, list[Change]]:
    """
    Return (new metadata, changes).

    `mapping` is what `read_mapping` returns. The input is not modified, and
    the changes list is empty when nothing on this ADO matched a filled row.
    """
    updated = dict(metadata)
    changes = []
    for key, rules in mapping.items():
        spec = keys[key]
        entries, single = as_entries(metadata.get(key))
        if not entries:
            continue
        rewritten, touched = [], False
        for entry in entries:
            rule = rules.get(identity(entry, spec))
            new_entry = _apply_rule(entry, spec, rule) if rule else entry
            touched = touched or new_entry != entry
            rewritten.append(new_entry)
        if not touched:
            continue
        rewritten = _dedupe(rewritten)
        updated[key] = rewritten[0] if single and len(rewritten) == 1 else rewritten
        changes.append(
            Change(
                key,
                " | ".join(display(entry, spec) for entry in entries),
                " | ".join(display(entry, spec) for entry in rewritten),
            )
        )
    return updated, changes


def collect_values(ados: Iterable[Ado], keys: dict[str, KeySpec] = KEYS) -> tuple[dict, int]:
    """
    Count distinct values per key across `ados`.

    Returns ({key: {identity: [ado count, first nid]}}, number of ADOs); an
    ADO holding a value twice counts once.
    """
    counts: dict[str, dict[tuple[str, ...], list]] = {key: {} for key in keys}
    total = 0
    for ado in ados:
        total += 1
        for key, spec in keys.items():
            entries, _single = as_entries(ado.metadata.get(key))
            for ident in {identity(entry, spec) for entry in entries}:
                if not any(ident):
                    continue
                if ident in counts[key]:
                    counts[key][ident][0] += 1
                else:
                    counts[key][ident] = [1, ado.nid]
    return counts, total


def _sort_key(ident: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(part.casefold() for part in ident) + ident


def write_workbook(path: Path, counts: dict, keys: dict[str, KeySpec] = KEYS, info: dict | None = None) -> None:
    # Values are data, never formulas, numbers or links: "=SUM(1)" stays text,
    # "1920" stays a string, and a URI does not eat into Excel's link limit.
    workbook = xlsxwriter.Workbook(
        str(path),
        {
            "strings_to_formulas": False,
            "strings_to_numbers": False,
            "strings_to_urls": False,
            "constant_memory": True,
        },
    )
    header_format = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#1F3864"})
    target_header_format = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#7F6000"})
    target_format = workbook.add_format({"bg_color": "#FFF2CC"})

    for key, spec in keys.items():
        sheet = workbook.add_worksheet(key)
        sources, targets = source_columns(spec), target_columns(spec)
        header = sources + targets + COUNT_COLUMNS
        for column, name in enumerate(header):
            sheet.write_string(0, column, name, target_header_format if name in targets else header_format)
            width = 14 if name in COUNT_COLUMNS else (24 if name.endswith("_role") else 50)
            sheet.set_column(column, column, width)

        rows = sorted(counts.get(key, {}), key=_sort_key)
        for row, ident in enumerate(rows, start=1):
            ado_count, nid = counts[key][ident]
            for column, part in enumerate(ident):
                sheet.write_string(row, column, part)
            for offset in range(len(targets)):
                sheet.write_blank(row, len(sources) + offset, None, target_format)
            sheet.write_number(row, len(header) - 2, ado_count)
            if nid is not None:
                sheet.write_number(row, len(header) - 1, nid)

        sheet.freeze_panes(1, 0)
        sheet.autofilter(0, 0, max(len(rows), 1), len(header) - 1)

    sheet = workbook.add_worksheet(INFO_SHEET)
    sheet.set_column(0, 0, 22)
    sheet.set_column(1, 1, 60)
    for row, (name, value) in enumerate((info or {}).items()):
        sheet.write_string(row, 0, name)
        sheet.write(row, 1, value)

    workbook.close()


def read_mapping(path: Path, keys: dict[str, KeySpec] = KEYS) -> Mapping:
    """
    Read a filled-in workbook into {key: {source identity: {part: new value}}}.

    Only rows with at least one target filled are kept, and each rule holds
    only the parts that were filled. Everything is checked before a single
    request is made: unknown sheets, missing columns, a target with no source,
    and a source given two different targets.
    """
    try:
        workbook = CalamineWorkbook.from_path(str(path))
    except Exception as error:
        raise MappingWorkbookError(f"cannot open {path}: {error}") from error

    try:
        mapping = _read_rules(workbook, keys)
    finally:
        # Released straight away, so Excel can reopen the file on Windows.
        workbook.close()
    if not mapping:
        raise MappingWorkbookError(f"no target values are filled in {path}; nothing to do")
    return mapping


def _read_rules(workbook: CalamineWorkbook, keys: dict[str, KeySpec]) -> Mapping:
    mapping: Mapping = {}
    for key in workbook.sheet_names:
        if key.startswith("_"):
            continue
        if key not in keys:
            raise MappingWorkbookError(f"sheet {key!r} is not a known key; expected one of: {', '.join(keys)}")
        spec = keys[key]
        rows = workbook.get_sheet_by_name(key).to_python()
        header = [clean(name) for name in rows[0]] if rows else []
        sources, targets = source_columns(spec), target_columns(spec)
        missing = [name for name in sources + targets if name not in header]
        if missing:
            raise MappingWorkbookError(f"sheet {key!r} is missing column(s): {', '.join(missing)}")
        source_at = [header.index(name) for name in sources]
        target_at = [header.index(name) for name in targets]

        rules: dict[tuple[str, ...], dict[str, str]] = {}
        first_row: dict[tuple[str, ...], int] = {}
        for row_number, row in enumerate(rows[1:], start=2):
            row = list(row) + [None] * (len(header) - len(row))
            rule = {
                part: clean(row[index]) for part, index in zip(spec.parts, target_at, strict=True) if clean(row[index])
            }
            if not rule:
                continue
            ident = tuple(clean(row[index]) for index in source_at)
            if not any(ident):
                raise MappingWorkbookError(f"sheet {key!r} row {row_number} has a target but no source")
            if all(rule.get(part, current) == current for part, current in zip(spec.parts, ident, strict=True)):
                continue
            if ident in rules and rules[ident] != rule:
                raise MappingWorkbookError(
                    f"sheet {key!r} rows {first_row[ident]} and {row_number} give the same source different targets"
                )
            rules[ident] = rule
            first_row.setdefault(ident, row_number)
        if rules:
            mapping[key] = rules
    return mapping


def get_metadata(
    output: Path,
    instance: Instance = Instance.local,
    client: ArchipelagoClient | None = None,
    track: Track = no_progress,
) -> GetResult:
    """
    Write the mapping workbook, one sheet for each of KEYS, from `instance`.

    Values come from the site's /api/metadata-values endpoint, a few large
    pages in all.
    """
    client = client or ArchipelagoClient(instance)
    client.require_auth()

    counts, total = collect_values(track(client.iter_metadata(KEYS), "Reading"), KEYS)
    write_workbook(
        output,
        counts,
        KEYS,
        info={
            "source": client.base,
            "generated": datetime.now(UTC).isoformat(timespec="seconds"),
            "ados with these keys": total,
        },
    )
    return GetResult(Path(output), total, {key: len(values) for key, values in counts.items()})


def find_changes(mapping: Mapping, client: ArchipelagoClient, track: Track = no_progress) -> list[Candidate]:
    """
    The ADOs a mapping would change.

    Reads only the mapped keys from the metadata endpoint and matches them in
    Python with the same apply_mapping the writes use, so the find and the
    writes agree on what counts as the same value. Collected in full before
    any write starts.
    """
    found = []
    for ado in track(client.iter_metadata(mapping), "Finding"):
        _new, changes = apply_mapping(ado.metadata, mapping)
        if changes:
            found.append(Candidate(ado, changes))
    return found


def _records(ado: Ado, changes: list[Change], status: str) -> Iterator[dict[str, Any]]:
    for change in changes:
        yield {
            "nid": ado.nid,
            "uuid": ado.uuid,
            "title": ado.title,
            "key": change.key,
            "old value": change.old,
            "new value": change.new,
            "status": status,
        }


def update_metadata(
    workbook: Path,
    instance: Instance = Instance.local,
    dry_run: bool = False,
    report_path: Path | None = Path("archipelago_mapping_report.csv"),
    client: ArchipelagoClient | None = None,
    track: Track = no_progress,
) -> UpdateResult:
    """
    Rewrite every ADO holding a source value from `workbook` to its target.

    The metadata endpoint says which ADOs hold a mapped value; only those are
    read and written through JSON:API, so Drupal still makes the revision,
    reindexes Solr and clears caches. Each one is read again right before it
    is written, so an edit made since the find is not thrown away.

    With `dry_run` only the find runs and nothing is written. The report goes
    to `report_path` either way.
    """
    mapping = read_mapping(workbook)
    client = client or ArchipelagoClient(instance)
    client.require_auth()

    candidates = find_changes(mapping, client, track)
    records: list[dict[str, Any]] = []
    updated = 0

    if dry_run:
        for candidate in candidates:
            records.extend(_records(candidate.ado, candidate.changes, "dry run"))
    else:
        for candidate in track(candidates, "Writing"):
            ado = candidate.ado
            try:
                fresh = client.get_ado(ado.bundle, ado.uuid)
            except Exception as error:
                records.extend(_records(ado, candidate.changes, f"error reading: {error}"))
                continue
            new, changes = apply_mapping(fresh.metadata, mapping)
            if not changes:
                # Already fixed since the find, by hand or by an earlier run.
                continue
            try:
                response = client.patch_metadata(fresh.bundle, fresh.uuid, new)
            except Exception as error:
                status = f"error writing: {error}"
            else:
                if response.status_code >= 400:
                    status = f"error writing: {response.status_code} {response.text[:200]}"
                else:
                    status = "ok"
                    updated += 1
            records.extend(_records(fresh, changes, status))

    if report_path:
        with open(report_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(records)

    return UpdateResult(records, len(candidates), updated, dry_run, Path(report_path) if report_path else None)
