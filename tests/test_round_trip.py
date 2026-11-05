import csv

import pytest
from conftest import make_workbook, node
from python_calamine import CalamineWorkbook

from value_mapper.client import ArchipelagoClient, ArchipelagoError, Instance, MissingCredentials
from value_mapper.mapping import KEYS, get_metadata, update_metadata

NODES = [
    node("a", 1, {"subject": ["Texas"], "agent": [{"value": "Smith, J.", "role": "author"}]}),
    node("b", 2, {"subject": "Maps"}),
    node("c", 3, {"label": "no mapped keys"}),
]


@pytest.fixture
def site(make_site):
    return make_site(NODES)


def test_get_writes_counts(site, client, tmp_path):
    result = get_metadata(tmp_path / "m.xlsx", client=client)
    assert result.total == 2
    assert result.distinct["subject"] == 2
    workbook = CalamineWorkbook.from_path(str(tmp_path / "m.xlsx"))
    try:
        rows = workbook.get_sheet_by_name("subject").to_python()
    finally:
        workbook.close()
    assert [row[0] for row in rows[1:]] == ["Maps", "Texas"]
    assert [(row[2], row[3]) for row in rows[1:]] == [(1, 2), (1, 1)]
    assert site.endpoint_calls[0]["keys"] == ",".join(KEYS)


def test_dry_run_sends_nothing(site, client, tmp_path):
    workbook = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "TX", 1, 1)]})
    result = update_metadata(workbook, dry_run=True, report_path=tmp_path / "r.csv", client=client)
    assert site.patches == []
    assert [(r["uuid"], r["title"], r["status"]) for r in result.records] == [("a", "t", "dry run")]
    assert (result.candidates, result.updated) == (1, 0)


def test_update_patches_only_changed_ados(site, client, tmp_path):
    workbook = make_workbook(
        tmp_path / "m.xlsx",
        {
            "subject": [("Texas", "TX", 1, 1)],
            "agent": [("Smith, J.", "author", None, "creator", 1, 1)],
        },
    )
    result = update_metadata(workbook, report_path=tmp_path / "r.csv", client=client)
    assert site.patches == [("a", {"subject": ["TX"], "agent": [{"value": "Smith, J.", "role": "creator"}]})]
    assert (result.updated, result.errors) == (1, 0)
    with open(tmp_path / "r.csv", newline="", encoding="utf-8") as handle:
        assert {(row["key"], row["status"]) for row in csv.DictReader(handle)} == {("subject", "ok"), ("agent", "ok")}


def test_only_mapped_keys_are_requested(site, client, tmp_path):
    workbook = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "TX", 1, 1)]})
    update_metadata(workbook, dry_run=True, report_path=None, client=client)
    assert site.endpoint_calls[0]["keys"] == "subject"


def test_ado_fixed_since_the_find_is_left_alone(site, client, tmp_path):
    site.nodes["a"] = node("a", 1, {"subject": ["TX"]})
    workbook = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "TX", 1, 1)]})
    result = update_metadata(workbook, report_path=None, client=client)
    assert site.patches == []
    assert result.records == []


def test_failed_write_is_reported_and_run_continues(make_site, client, tmp_path):
    site = make_site(
        [node("a", 1, {"subject": ["Texas"]}), node("b", 2, {"subject": ["Texas"]})], patch_status={"a": 405}
    )
    workbook = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "TX", 1, 1)]})
    result = update_metadata(workbook, report_path=None, client=client)
    status = {r["uuid"]: r["status"] for r in result.records}
    assert status["a"].startswith("error writing: 405")
    assert status["b"] == "ok"
    assert (result.updated, result.errors) == (1, 1)
    assert [uuid for uuid, _ in site.patches] == ["a", "b"]


def test_missing_credentials_refused_before_any_request(http, tmp_path):
    workbook = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "TX", 1, 1)]})
    with pytest.raises(MissingCredentials):
        update_metadata(workbook, report_path=None, client=ArchipelagoClient(Instance.prod))
    assert len(http.calls) == 0


def unpublished_site(make_site):
    return make_site([node("a", 1, {"subject": ["Texas"]}), node("b", 2, {"subject": ["Texas"]}, published=False)])


def test_unpublished_ados_are_skipped_by_default(make_site, client, tmp_path):
    site = unpublished_site(make_site)
    workbook = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "TX", 1, 1)]})
    result = update_metadata(workbook, report_path=tmp_path / "r.csv", client=client)
    assert [uuid for uuid, _ in site.patches] == ["a"]
    assert (result.candidates, result.updated, result.unpublished, result.errors) == (1, 1, 1, 0)
    with open(tmp_path / "r.csv", newline="", encoding="utf-8") as handle:
        assert {row["uuid"]: row["status"] for row in csv.DictReader(handle)} == {
            "a": "ok",
            "b": "skipped: not published",
        }


def test_dry_run_reports_unpublished_as_skipped(make_site, client, tmp_path):
    site = unpublished_site(make_site)
    workbook = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "TX", 1, 1)]})
    result = update_metadata(workbook, dry_run=True, report_path=None, client=client)
    assert site.patches == []
    assert {r["uuid"]: r["status"] for r in result.records} == {"a": "dry run", "b": "skipped: not published"}
    assert (result.candidates, result.unpublished) == (1, 1)


def test_include_unpublished_writes_them_too(make_site, client, tmp_path):
    site = unpublished_site(make_site)
    workbook = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "TX", 1, 1)]})
    result = update_metadata(workbook, report_path=None, client=client, include_unpublished=True)
    assert [uuid for uuid, _ in site.patches] == ["a", "b"]
    assert (result.updated, result.unpublished) == (2, 0)
    assert site.nodes["b"]["attributes"]["status"] is False


def test_ado_unpublished_since_the_find_is_skipped(site, client, tmp_path):
    site.nodes["a"] = node("a", 1, {"subject": ["Texas"]}, published=False)
    workbook = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "TX", 1, 1)]})
    result = update_metadata(workbook, report_path=None, client=client)
    assert site.patches == []
    assert [r["status"] for r in result.records] == ["skipped: not published"]
    assert (result.updated, result.unpublished) == (0, 1)


def test_site_that_does_not_report_published_is_refused(make_site, client, tmp_path):
    site = make_site([node("a", 1, {"subject": ["Texas"]})], reports_published=False)
    workbook = make_workbook(tmp_path / "m.xlsx", {"subject": [("Texas", "TX", 1, 1)]})
    with pytest.raises(ArchipelagoError, match="update the tamu_metadata_api module"):
        update_metadata(workbook, report_path=None, client=client)
    assert site.patches == []
    result = update_metadata(workbook, report_path=None, client=client, include_unpublished=True)
    assert result.updated == 1
