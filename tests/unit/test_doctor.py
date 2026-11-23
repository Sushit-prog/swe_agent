"""Unit tests for `swe doctor`."""

from typer.testing import CliRunner

from swe_agent.cli import main as cli_main
from swe_agent.config.schema import SweAgentConfig

runner = CliRunner()


def test_doctor_fails_when_git_missing(monkeypatch) -> None:
    """doctor must report FAIL and exit non-zero when git is not on PATH."""

    monkeypatch.setattr(cli_main.shutil, "which", lambda name: None)

    result = runner.invoke(cli_main.app, ["doctor"])

    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "git binary" in result.output
    assert "not found on PATH" in result.output


def test_doctor_passes_when_git_present(monkeypatch) -> None:
    """Sanity: with real git available, doctor exits 0 on this machine."""
    import shutil

    if shutil.which("git") is None:
        import pytest

        pytest.skip("git not installed on this machine")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    result = runner.invoke(cli_main.app, ["doctor"])
    assert result.exit_code == 0, result.output
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


def test_doctor_reports_api_key_env_without_leaking_value(monkeypatch) -> None:
    secret = "sk-ant-DOCTOR-SENTINEL-DO-NOT-PRINT"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: SweAgentConfig())

    result = runner.invoke(cli_main.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "API key env vars" in result.output
    assert "ANTHROPIC_API_KEY" in result.output
    assert secret not in result.output


def test_doctor_fails_when_api_key_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: SweAgentConfig())

    result = runner.invoke(cli_main.app, ["doctor"])

    assert result.exit_code == 1
    assert "API key env vars" in result.output
    assert "ANTHROPIC_API_KEY" in result.output
    assert "not set" in result.output


def test_doctor_uses_configured_api_key_env_name(monkeypatch) -> None:
    cfg = SweAgentConfig(models={"planner": {"api_key_env": "CUSTOM_MODEL_KEY"}})
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: cfg)
    monkeypatch.delenv("CUSTOM_MODEL_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-default")

    result = runner.invoke(cli_main.app, ["doctor"])

    assert result.exit_code == 1
    assert "CUSTOM_MODEL_KEY" in result.output
