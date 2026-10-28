"""Typer application for the `swe` command."""

from __future__ import annotations

import shutil
import subprocess
import sys

import typer
from rich.console import Console
from rich.table import Table

from swe_agent import __version__

app = typer.Typer(
    name="swe",
    help="swe-agent: CLI-first autonomous Software Engineering Agent.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"swe-agent {__version__} (Python {sys.version.split()[0]})")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """swe-agent CLI entry point."""


@app.command()
def doctor() -> None:
    """Check that the local environment can run swe-agent."""
    checks: list[tuple[str, bool, str]] = []

    # Python version
    py_ok = sys.version_info >= (3, 11)
    py_detail = f"{sys.version.split()[0]} (requires >= 3.11)"
    checks.append(("Python version", py_ok, py_detail))

    # Git availability
    git_path = shutil.which("git")
    if git_path is None:
        checks.append(("git binary", False, "not found on PATH"))
    else:
        try:
            proc = subprocess.run(
                [git_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ok = proc.returncode == 0
            detail = proc.stdout.strip() if ok else f"exit code {proc.returncode}"
            checks.append(("git binary", ok, detail))
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append(("git binary", False, f"failed to execute: {exc}"))

    table = Table(title="swe-agent doctor")
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail")

    all_ok = True
    for name, ok, detail in checks:
        status = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
        if not ok:
            all_ok = False
        table.add_row(name, status, detail)

    console.print(table)

    if not all_ok:
        err_console.print("[red]Some checks failed.[/red]")
        raise typer.Exit(code=1)

    console.print("[green]Environment looks good.[/green]")


if __name__ == "__main__":
    app()
