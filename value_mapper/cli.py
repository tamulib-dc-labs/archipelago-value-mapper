"""The value-mapper command line: ``value-mapper get`` and ``value-mapper update``."""

from collections.abc import Iterable, Iterator
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn, TypeVar

import requests
import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from .client import ArchipelagoError, Instance
from .mapping import KEYS, MappingWorkbookError, get_metadata, update_metadata

T = TypeVar("T")

app = typer.Typer(
    help="Clean up name, subject and genre values across Archipelago Digital Objects through a spreadsheet.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
errors = Console(stderr=True)

KeyName = StrEnum("KeyName", {key: key for key in KEYS})

InstanceOption = Annotated[
    Instance, typer.Option("--instance", "-i", help="Which site: local (http://localhost:8001) or prod.")
]


def track(items: Iterable[T], label: str) -> Iterator[T]:
    """A Rich progress bar: a count for a stream of unknown length, a bar for a list."""
    total = len(items) if hasattr(items, "__len__") else None
    columns = [SpinnerColumn(), TextColumn("{task.description}")]
    columns += [BarColumn(), MofNCompleteColumn()] if total is not None else [TextColumn("{task.completed} ADOs")]
    with Progress(*columns, TimeElapsedColumn(), console=errors) as progress:
        task = progress.add_task(label, total=total)
        for item in items:
            yield item
            progress.advance(task)


def fail(error: Exception) -> NoReturn:
    errors.print(f"[bold red]Error:[/] {error}", highlight=False)
    raise typer.Exit(1)


@app.command()
def get(
    output: Annotated[Path, typer.Option("--output", "-o", help="Workbook to write.")] = Path(
        "archipelago_mapping.xlsx"
    ),
    instance: InstanceOption = Instance.local,
    key: Annotated[
        list[KeyName] | None, typer.Option("--key", "-k", help="Only this key (repeatable). Default: all of them.")
    ] = None,
) -> None:
    """Write a workbook of every value, one sheet per key, with empty target columns to fill in."""
    try:
        result = get_metadata(output, instance, [k.value for k in key] if key else None, track=track)
    except (ArchipelagoError, requests.RequestException) as error:
        fail(error)

    table = Table("key", "distinct values", box=None)
    for name, number in result.distinct.items():
        table.add_row(name, str(number))
    console.print(table)
    console.print(f"Read {result.total} ADO(s); wrote [bold]{result.path}[/]")


@app.command()
def update(
    workbook: Annotated[
        Path, typer.Option("--xlsx", "-x", exists=True, dir_okay=False, help="The filled-in workbook from `get`.")
    ],
    instance: InstanceOption = Instance.local,
    report: Annotated[Path, typer.Option("--output", "-o", help="Report CSV to write.")] = Path(
        "archipelago_mapping_report.csv"
    ),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report what would change; write nothing.")] = False,
) -> None:
    """Rewrite values to the targets filled in the workbook. Rows with every target blank are ignored."""
    try:
        result = update_metadata(workbook, instance, dry_run, report, track=track)
    except (MappingWorkbookError, ArchipelagoError, requests.RequestException) as error:
        fail(error)

    if result.dry_run:
        console.print(f"Would update {result.candidates} ADO(s)")
    else:
        console.print(f"Updated {result.updated} of {result.candidates} ADO(s)")
    if result.errors:
        errors.print(f"[bold red]{result.errors} change(s) failed[/]; see the status column in the report")
    console.print(f"Wrote report to [bold]{result.report_path}[/]")
    if result.errors:
        raise typer.Exit(1)
