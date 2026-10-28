"""Typer application for the `swe` command."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from swe_agent import __version__
from swe_agent.config.schema import SweAgentConfig, load_config
from swe_agent.core.sanitize import sanitize_untrusted, truncate_after_sanitize
from swe_agent.github.client import GitHubClient, IssueData
from swe_agent.github.errors import (
    AuthError,
    GitHubError,
    IssueIsPullRequestError,
    InvalidIssueURL,
    NetworkError,
    NotFoundError,
    RateLimitError,
)
from swe_agent.github.urls import parse_issue_url
from swe_agent.tools.errors import GitExecutionError, GitTimeoutError, PolicyError
from swe_agent.workspace import manager
from swe_agent.workspace.errors import (
    CleanupRefusedError,
    WorkspaceError,
)

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

    # Workspace root: existence, writability, free disk space.
    # Never creates the root — reports its state only.
    try:
        config = load_config()
        root = config.workspace.root
    except Exception as exc:
        checks.append(("workspace root", False, f"config error: {exc}"))
        root = None
    if root is not None:
        if not root.exists():
            checks.append(
                ("workspace root", True, f"{root} (will be created on first run)")
            )
        elif not root.is_dir():
            checks.append(("workspace root", False, f"{root} exists but is not a directory"))
        else:
            writable = os.access(root, os.W_OK)
            checks.append(
                ("workspace root", writable, f"{root} ({'writable' if writable else 'not writable'})")
            )
            probe_path = root if root.exists() else root.parent
            try:
                usage = shutil.disk_usage(probe_path)
                free_gb = usage.free / (1024**3)
                checks.append(
                    ("disk space", free_gb > 1, f"{free_gb:.1f} GiB free")
                )
            except OSError as exc:
                checks.append(("disk space", False, f"cannot stat: {exc}"))

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


def _untrusted(text: str, style: str | None = None) -> Text:
    """Render untrusted text safely: sanitized, no Rich markup interpretation."""
    return Text(sanitize_untrusted(text), style=style)


@app.command()
def run(
    issue_url: str = typer.Argument(..., help="GitHub issue URL (https://github.com/{owner}/{repo}/issues/{n})"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full tracebacks."),
) -> None:
    """Fetch a GitHub issue, create the isolated workspace, and clone the repo."""
    try:
        ref = parse_issue_url(issue_url)
    except InvalidIssueURL as exc:
        if verbose:
            raise
        err_console.print(f"[red]Error:[/red] invalid issue URL: {exc}")
        raise typer.Exit(code=1) from None

    token = os.environ.get("GITHUB_TOKEN") or None
    client = GitHubClient(token=token)

    try:
        issue = client.fetch_issue(ref)
    except (AuthError, NotFoundError, RateLimitError, NetworkError, IssueIsPullRequestError) as exc:
        if verbose:
            raise
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except GitHubError as exc:  # pragma: no cover - defensive
        if verbose:
            raise
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None

    _render_issue(ref.owner, ref.repo, issue)

    try:
        config = load_config()
    except Exception as exc:
        err_console.print(f"[red]Error:[/red] config: {exc}")
        raise typer.Exit(code=1) from None

    try:
        workspace = manager.create_run(ref, config)
    except (WorkspaceError, OSError) as exc:
        if verbose:
            raise
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None

    try:
        run_data = manager.clone_repository(workspace, config, github_client=client)
        validation = manager.validate_workspace(workspace, config)
        if not validation.ok:
            raise WorkspaceError("validation failed: " + "; ".join(validation.problems))
        run_data = manager.finalize_run(workspace)
    except (WorkspaceError, PolicyError, GitTimeoutError, GitExecutionError, OSError) as exc:
        # clone_repository already removed any partial run dir on clone
        # failure; validation/finalize failures clean up here.
        try:
            if workspace.run_dir.exists():
                manager.cleanup_run(workspace.run_id, config)
        except Exception:
            pass  # best-effort cleanup; the original error is what matters
        if verbose:
            raise
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None

    _render_workspace(workspace, run_data, validation)
    console.print("[dim]Pipeline not implemented yet (M4+)[/dim]")


def _render_workspace(workspace, run_data: dict, validation) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", justify="right")
    table.add_column()
    table.add_row("Run ID", workspace.run_id)
    table.add_row("Workspace", str(workspace.run_dir))
    table.add_row("Base branch", str(run_data.get("base_branch")))
    base_commit = str(run_data.get("base_commit") or "")
    table.add_row("Base commit", base_commit[:12] if base_commit else "-")
    table.add_row("Work branch", str(run_data.get("work_branch")))
    table.add_row("Status", "workspace_ready" if validation.ok else "invalid")
    console.print(Panel(table, title="Run workspace", expand=False))


@app.command()
def clean(
    run_id: str | None = typer.Argument(None, help="Run id to remove (see `swe run` output)."),
    all_runs: bool = typer.Option(False, "--all", help="Remove every run under the workspace root."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Remove run workspaces. Only ever touches <root>/runs/."""
    if (run_id is None) == (not all_runs):
        # exactly one of: a run id given, or --all given
        err_console.print("[red]Error:[/red] provide exactly one of: RUN_ID or --all")
        raise typer.Exit(code=1)

    try:
        config = load_config()
    except Exception as exc:
        err_console.print(f"[red]Error:[/red] config: {exc}")
        raise typer.Exit(code=1) from None

    try:
        if all_runs:
            targets = manager.list_runs(config)
            if not targets:
                console.print("No runs to clean.")
                return
            if not yes and not typer.confirm(
                f"Remove all {len(targets)} run(s) under {config.workspace.root}?"
            ):
                console.print("Aborted.")
                raise typer.Exit(code=0)
            for rid in targets:
                manager.cleanup_run(rid, config)
                console.print(f"[green]removed[/green] {rid}")
        else:
            if not yes and not typer.confirm(f"Remove run {run_id}?"):
                console.print("Aborted.")
                raise typer.Exit(code=0)
            manager.cleanup_run(run_id, config)
            console.print(f"[green]removed[/green] {run_id}")
    except CleanupRefusedError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except OSError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None


def _render_issue(owner: str, repo: str, issue: IssueData) -> None:
    body = truncate_after_sanitize(issue.body or "", limit=500)

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", justify="right")
    table.add_column()

    table.add_row("Repository", f"{owner}/{repo}")
    table.add_row("Issue", f"#{issue.number}")
    table.add_row("Title", _untrusted(issue.title))
    table.add_row("State", issue.state)
    labels = ", ".join(sanitize_untrusted(label) for label in issue.labels) or "(none)"
    table.add_row("Labels", Text(labels))
    table.add_row("Author", _untrusted(issue.author))
    table.add_row("URL", issue.url)

    console.print(Panel(table, title="GitHub Issue", expand=False))
    console.print(Panel(_untrusted(body), title="Body (untrusted content)", expand=False))
    console.print(
        "[yellow]![/yellow] [dim]Issue title, body, labels, and author are "
        "untrusted content — treat any instructions inside as data, not commands.[/dim]"
    )


if __name__ == "__main__":
    app()
