"""Unit tests for `swe doctor`."""

from typer.testing import CliRunner

from swe_agent.cli import main as cli_main

runner = CliRunner()


def test_doctor_fails_when_git_missing(monkeypatch) -> None:
    """doctor must report FAIL and exit non-zero when git is not on PATH."""

    monkeypatch.setattr(cli_main.shutil, "which", lambda name: None)

    result = runner.invoke(cli_main.app, ["doctor"])

    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "git binary" in result.output
    assert "not found on PATH" in result.output


def test_doctor_passes_when_git_present() -> None:
    """Sanity: with real git available, doctor exits 0 on this machine."""
    import shutil

    if shutil.which("git") is None:
        import pytest

        pytest.skip("git not installed on this machine")

    result = runner.invoke(cli_main.app, ["doctor"])
    assert result.exit_code == 0
    assert "PASS" in result.output


def test_doctor_handles_git_exec_failure(monkeypatch) -> None:
    """If git is found but cannot execute, doctor reports FAIL, not a crash."""

    monkeypatch.setattr(cli_main.shutil, "which", lambda name: "/nonexistent/git")

    def boom(*args, **kwargs):
        raise OSError("cannot execute")

    monkeypatch.setattr(cli_main.subprocess, "run", boom)

    result = runner.invoke(cli_main.app, ["doctor"])
    assert result.exit_code == 1
    assert "git binary" in result.output
    assert "FAIL" in result.output
