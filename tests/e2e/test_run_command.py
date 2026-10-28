"""CLI tests for `swe run`: success path and one exit code per typed error."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from swe_agent.cli import main as cli_main
from swe_agent.github.client import IssueData
from swe_agent.github.errors import (
    AuthError,
    IssueIsPullRequestError,
    NetworkError,
    NotFoundError,
    RateLimitError,
)

runner = CliRunner()

URL = "https://github.com/o/r/issues/7"


def _issue(**overrides) -> IssueData:
    base = dict(
        number=7,
        title="Fix the bug",
        body="Body text here.",
        state="open",
        labels=["bug", "urgent"],
        author="octocat",
        url="https://github.com/o/r/issues/7",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_pull_request=False,
    )
    base.update(overrides)
    return IssueData(**base)


def _client_returning(issue: IssueData):
    class _Client:
        def __init__(self, token=None) -> None:
            pass

        def fetch_issue(self, ref):
            return issue

    return _Client


def _client_raising(exc: Exception):
    class _Client:
        def __init__(self, token=None) -> None:
            pass

        def fetch_issue(self, ref):
            raise exc

    return _Client


def test_success_path(monkeypatch) -> None:
    monkeypatch.setattr(cli_main, "GitHubClient", _client_returning(_issue()))
    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(cli_main.app, ["run", URL])
    assert result.exit_code == 0
    assert "o/r" in result.output
    assert "#7" in result.output
    assert "Fix the bug" in result.output
    assert "open" in result.output
    assert "bug" in result.output and "urgent" in result.output
    assert "Body text here." in result.output
    assert "untrusted" in result.output.lower()
    assert "Pipeline not implemented yet (M3+)" in result.output
    assert "Traceback" not in result.output


def test_body_truncated_after_500_chars(monkeypatch) -> None:
    issue = _issue(body="A" * 600)
    monkeypatch.setattr(cli_main, "GitHubClient", _client_returning(issue))
    monkeypatch.setenv("COLUMNS", "400")
    result = runner.invoke(cli_main.app, ["run", URL])
    assert result.exit_code == 0
    assert "truncated" in result.output
    assert "A" * 600 not in result.output


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/o/r/issues/7",
        "https://gitlab.com/o/r/issues/7",
        "https://github.com/o/r/pull/7",
        "https://github.com/o/r/issues/77abc",
    ],
)
def test_invalid_url_exits_1_clean(url) -> None:
    result = runner.invoke(cli_main.app, ["run", url])
    assert result.exit_code == 1
    assert "invalid issue URL" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "exc, fragment",
    [
        (
            AuthError("authentication failed: check GITHUB_TOKEN"),
            "authentication failed",
        ),
        (NotFoundError("issue o/r#7 not found"), "not found"),
        (RateLimitError("GitHub API rate limit exceeded"), "rate limit"),
        (NetworkError("network error contacting GitHub: boom"), "network error"),
        (IssueIsPullRequestError("o/r#7 is a pull request"), "pull request"),
    ],
)
def test_each_typed_error_exits_1_clean(monkeypatch, exc, fragment) -> None:
    monkeypatch.setattr(cli_main, "GitHubClient", _client_raising(exc))
    result = runner.invoke(cli_main.app, ["run", URL])
    assert result.exit_code == 1
    assert fragment in result.output
    assert "Traceback" not in result.output


def test_verbose_flag_still_exits_1(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_main,
        "GitHubClient",
        _client_raising(AuthError("authentication failed")),
    )
    result = runner.invoke(cli_main.app, ["run", URL, "-v"])
    assert result.exit_code == 1


def test_help_includes_run() -> None:
    result = runner.invoke(cli_main.app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.output
