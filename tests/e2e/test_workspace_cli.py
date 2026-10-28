"""E2E CLI tests for the M3 workspace phase: run summary, clean, doctor.

GitHubClient and the clone are mocked — no network.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from swe_agent.cli import main as cli_main
from swe_agent.config.schema import SweAgentConfig
from swe_agent.github.client import IssueData
from swe_agent.github.urls import IssueRef
from swe_agent.workspace import manager
from swe_agent.workspace.errors import CloneError

runner = CliRunner()

URL = "https://github.com/o/r/issues/7"


def _issue() -> IssueData:
    return IssueData(
        number=7,
        title="Fix the bug",
        body="Body text here.",
        state="open",
        labels=["bug"],
        author="octocat",
        url=URL,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_pull_request=False,
    )


class _FakeClient:
    def __init__(self, token=None) -> None:
        pass

    def fetch_issue(self, ref):
        return _issue()

    def get_repo_size_mb(self, owner, repo):
        return 1.0


def _config(tmp_path: Path) -> SweAgentConfig:
    return SweAgentConfig(workspace={"root": str(tmp_path / "ws-root")})


def _setup(monkeypatch, tmp_path: Path, *, fail_validation: bool = False):
    monkeypatch.setattr(cli_main, "GitHubClient", _FakeClient)
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: _config(tmp_path))

    # Real create_run/finalize; mocked clone + validation.
    def fake_clone(ws, config, github_client=None, **kwargs):
        # Simulate a successful clone: build a real git repo with a real
        # commit (tests use raw subprocess — the policy applies to agent-run
        # code, not test setup).
        import subprocess

        def _git(args):
            subprocess.run(
                ["git", *args],
                cwd=str(ws.repository),
                capture_output=True,
                timeout=30,
                check=True,
                env={
                    **__import__("os").environ,
                    "GIT_AUTHOR_NAME": "T",
                    "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "T",
                    "GIT_COMMITTER_EMAIL": "t@t",
                },
            )

        _git(["init", "-b", "main"])
        # Disable CRLF normalization for the fixture repo: Python writes and
        # git commits happen within one mtime tick, which on Windows with
        # autocrlf=true makes an immediate `git status` flaky (racy stat).
        _git(["config", "core.autocrlf", "false"])
        (ws.repository / "file.txt").write_bytes(b"x\n")
        _git(["add", "file.txt"])
        _git(["commit", "-m", "init"])
        # Warm git's stat cache so the validation status check is deterministic.
        subprocess.run(
            ["git", "status"], cwd=str(ws.repository), capture_output=True, timeout=30
        )
        data = json.loads(ws.run_json_path.read_text(encoding="utf-8"))
        work_branch = f"swe-agent/issue-7-{ws.run_id}"
        data.update(
            base_branch="main",
            base_commit="a" * 40,
            work_branch=work_branch,
        )
        ws.run_json_path.write_text(json.dumps(data), encoding="utf-8")
        _git(["switch", "-c", work_branch])
        return data

    monkeypatch.setattr(cli_main.manager, "clone_repository", fake_clone)

    if fail_validation:
        from swe_agent.workspace.manager import WorkspaceValidation

        monkeypatch.setattr(
            cli_main.manager,
            "validate_workspace",
            lambda ws, config=None: WorkspaceValidation(
                ok=False, problems=["working tree is not clean"]
            ),
        )
    monkeypatch.setenv("COLUMNS", "200")
    return runner.invoke(cli_main.app, ["run", URL])


def test_run_creates_workspace_and_prints_summary(monkeypatch, tmp_path: Path) -> None:
    result = _setup(monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert "Run ID" in result.output
    assert "workspace_ready" in result.output
    assert "swe-agent/issue-7-" in result.output
    assert "Pipeline not implemented yet (M4+)" in result.output
    assert "Traceback" not in result.output

    # run.json exists with status flipped to workspace_ready
    runs = tmp_path / "ws-root" / "runs"
    run_dirs = [p for p in runs.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    data = json.loads((run_dirs[0] / "state" / "run.json").read_text(encoding="utf-8"))
    assert data["status"] == "workspace_ready"
    assert data["clone_url"] == "https://github.com/o/r.git"


def test_run_validation_failure_cleans_up_and_exits_1(
    monkeypatch, tmp_path: Path
) -> None:
    result = _setup(monkeypatch, tmp_path, fail_validation=True)
    assert result.exit_code == 1
    assert "validation failed" in result.output
    assert "Traceback" not in result.output
    runs = tmp_path / "ws-root" / "runs"
    assert not runs.exists() or not any(runs.iterdir()), "partial run must be removed"


def test_run_clone_failure_cleans_up_and_exits_1(
    monkeypatch, tmp_path: Path
) -> None:
    def failing_clone(ws, config, github_client=None, **kwargs):
        # mimic manager behavior: remove dir, raise typed error
        manager._force_rmtree(ws.run_dir)
        raise CloneError("git clone failed (exit 128): repository not found")

    monkeypatch.setattr(cli_main, "GitHubClient", _FakeClient)
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: _config(tmp_path))
    monkeypatch.setattr(cli_main.manager, "clone_repository", failing_clone)
    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(cli_main.app, ["run", URL])
    assert result.exit_code == 1
    assert "repository not found" in result.output
    assert "Traceback" not in result.output
    runs = tmp_path / "ws-root" / "runs"
    assert not runs.exists() or not any(runs.iterdir())


# ------------------------------------------------------------------ clean


def test_clean_single_run(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: _config(tmp_path))
    ref = IssueRef(owner="o", repo="r", number=7)
    ws = manager.create_run(ref, _config(tmp_path))
    assert ws.run_dir.exists()
    result = runner.invoke(cli_main.app, ["clean", ws.run_id, "--yes"])
    assert result.exit_code == 0, result.output
    assert "removed" in result.output
    assert not ws.run_dir.exists()


def test_clean_requires_confirmation_without_yes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: _config(tmp_path))
    ref = IssueRef(owner="o", repo="r", number=7)
    ws = manager.create_run(ref, _config(tmp_path))
    result = runner.invoke(
        cli_main.app, ["clean", ws.run_id], input="n\n"
    )
    assert result.exit_code == 0
    assert "Aborted" in result.output
    assert ws.run_dir.exists(), "declined confirmation must not delete"


def test_clean_confirmed_interactive(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: _config(tmp_path))
    ref = IssueRef(owner="o", repo="r", number=7)
    ws = manager.create_run(ref, _config(tmp_path))
    result = runner.invoke(cli_main.app, ["clean", ws.run_id], input="y\n")
    assert result.exit_code == 0, result.output
    assert not ws.run_dir.exists()


def test_clean_all(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: _config(tmp_path))
    ref = IssueRef(owner="o", repo="r", number=7)
    a = manager.create_run(ref, _config(tmp_path))
    b = manager.create_run(ref, _config(tmp_path))
    result = runner.invoke(cli_main.app, ["clean", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    assert not a.run_dir.exists() and not b.run_dir.exists()


def test_clean_all_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: _config(tmp_path))
    result = runner.invoke(cli_main.app, ["clean", "--all", "--yes"])
    assert result.exit_code == 0
    assert "No runs" in result.output


def test_clean_requires_id_or_all(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: _config(tmp_path))
    result = runner.invoke(cli_main.app, ["clean"])
    assert result.exit_code == 1
    assert "exactly one" in result.output
    result = runner.invoke(cli_main.app, ["clean", "some-id", "--all", "--yes"])
    assert result.exit_code == 1


def test_clean_refuses_traversal(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: _config(tmp_path))
    result = runner.invoke(cli_main.app, ["clean", "..", "--yes"])
    assert result.exit_code == 1
    assert "refusing" in result.output.lower() or "Error" in result.output


def test_clean_only_touches_runs_dir(monkeypatch, tmp_path: Path) -> None:
    """A sibling directory outside runs/ must survive --all."""
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: _config(tmp_path))
    cfg = _config(tmp_path)
    manager.create_run(IssueRef(owner="o", repo="r", number=1), cfg)
    sibling = cfg.workspace.root / "important-data"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "keep.txt").write_text("keep", encoding="utf-8")
    result = runner.invoke(cli_main.app, ["clean", "--all", "--yes"])
    assert result.exit_code == 0
    assert (sibling / "keep.txt").exists()
    assert not (cfg.workspace.root / "runs").exists() or not any(
        (cfg.workspace.root / "runs").iterdir()
    )


# ----------------------------------------------------------------- doctor


def test_doctor_does_not_create_workspace_root(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "not-created-yet"
    cfg = SweAgentConfig(workspace={"root": str(root)})
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: cfg)
    result = runner.invoke(cli_main.app, ["doctor"])
    # doctor may PASS (reports "will be created") or fail on other checks,
    # but must never mkdir the root itself
    assert not root.exists(), "doctor must not create the workspace root"
    assert "workspace root" in result.output


def test_doctor_reports_disk_space(monkeypatch, tmp_path: Path) -> None:
    cfg = SweAgentConfig(workspace={"root": str(tmp_path / "ws")})
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(cli_main, "load_config", lambda path=None: cfg)
    result = runner.invoke(cli_main.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "disk space" in result.output
    assert "GiB free" in result.output
