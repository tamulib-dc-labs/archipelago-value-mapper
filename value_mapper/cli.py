"""The value-mapper command line: ``value-mapper get`` and ``value-mapper update``."""

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Annotated, NoReturn, TypeVar

import requests
import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from .client import ArchipelagoError, Instance
from .mapping import MappingWorkbookError, get_metadata, update_metadata

T = TypeVar("T")

app = typer.Typer(
    help="Clean up name, subject and genre values across Archipelago Digital Objects through a spreadsheet.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
errors = Console(stderr=True)

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


def say(target: Console, text: str) -> None:
    # Soft wrap: let the terminal wrap long lines rather than Rich breaking
    # them at 80 columns, which splits paths and messages when not on a TTY.
    target.print(text, soft_wrap=True, highlight=False)


def fail(error: Exception) -> NoReturn:
    # Escaped: values such as ["Utah" would otherwise be read as Rich markup.
    say(errors, f"[bold red]Error:[/] {escape(str(error))}")
    raise typer.Exit(1)


@app.command()
def get(
    output: Annotated[Path, typer.Option("--output", "-o", help="Workbook to write.")] = Path(
        "archipelago_mapping.xlsx"
    ),
    instance: InstanceOption = Instance.local,
) -> None:
    """Write a workbook of every value, one sheet per key, with empty target columns to fill in."""
    try:
        result = get_metadata(output, instance, track=track)
    except (ArchipelagoError, requests.RequestException) as error:
        fail(error)

    table = Table("key", "distinct values", box=None)
    for name, number in result.distinct.items():
        table.add_row(name, str(number))
    console.print(table)
    say(console, f"Read {result.total} ADO(s); wrote [bold]{escape(str(result.path))}[/]")


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
    include_unpublished: Annotated[
        bool,
        typer.Option("--include-unpublished", help="Also update unpublished ADOs; by default they are left alone."),
    ] = False,
) -> None:
    """
    Rewrite values to the targets filled in the workbook. Rows with every target blank are ignored.

    Only published ADOs are updated unless --include-unpublished is given.
    """
    try:
        result = update_metadata(
            workbook, instance, dry_run, report, track=track, include_unpublished=include_unpublished
        )
    except (MappingWorkbookError, ArchipelagoError, requests.RequestException) as error:
        fail(error)

    if result.dry_run:
        say(console, f"Would update {result.candidates} ADO(s)")
    else:
        say(console, f"Updated {result.updated} of {result.candidates} ADO(s)")
    if result.unpublished:
        say(
            console,
            f"Skipped {result.unpublished} unpublished ADO(s); pass --include-unpublished to update them too",
        )
    if result.errors:
        say(errors, f"[bold red]{result.errors} change(s) failed[/]; see the status column in the report")
    say(console, f"Wrote report to [bold]{escape(str(result.report_path))}[/]")
    if result.errors:
        raise typer.Exit(1)
