"""Smoke test: the CLI entry points must run without error."""

from typer.testing import CliRunner

from swe_agent.cli.main import app

runner = CliRunner()


def test_help_runs_without_error() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_doctor_runs_without_error() -> None:
    result = runner.invoke(app, ["doctor"])
    # On a healthy machine git+python both pass (exit 0); the invariant is
    # that the command itself runs and reports rather than crashing.
    assert result.exit_code in (0, 1)
    assert "doctor" in result.output.lower()
    assert "Python version" in result.output
    assert "git binary" in result.output
