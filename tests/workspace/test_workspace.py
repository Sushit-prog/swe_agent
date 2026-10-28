"""Workspace layout, path containment, validation, and cleanup tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from swe_agent.config.schema import SweAgentConfig
from swe_agent.github.urls import IssueRef
from swe_agent.workspace import manager
from swe_agent.workspace.errors import (
    CleanupRefusedError,
    WorkspaceEscapeError,
)
from swe_agent.workspace.paths import is_within, resolve_within

REF = IssueRef(owner="o", repo="r", number=7)


def _config(root: Path) -> SweAgentConfig:
    return SweAgentConfig(workspace={"root": str(root)})


# ---------------------------------------------------------------- layout


def test_create_run_layout(tmp_path: Path) -> None:
    ws = manager.create_run(REF, _config(tmp_path))
    assert ws.run_dir.is_dir()
    for sub in ("repository", "state", "logs", "traces", "artifacts"):
        assert (ws.run_dir / sub).is_dir()
    assert (ws.state / "hooks").is_dir()
    assert ws.run_json_path.is_file()

    import json

    data = json.loads(ws.run_json_path.read_text(encoding="utf-8"))
    assert data["status"] == "creating"
    assert data["issue"] == {"owner": "o", "repo": "r", "number": 7}
    assert data["clone_url"] == "https://github.com/o/r.git"
    assert data["base_branch"] is None


def test_run_ids_unique_and_sortable(tmp_path: Path) -> None:
    ids = [manager.create_run(REF, _config(tmp_path)).run_id for _ in range(20)]
    assert len(set(ids)) == 20
    # timestamp prefix is non-decreasing across creation (sortable);
    # within the same second the random suffix decides order
    stamps = [rid[:16] for rid in ids]  # YYYYMMDDTHHMMSSZ
    assert stamps == sorted(stamps)
    for rid in ids:
        stamp, _, hexpart = rid.rpartition("-")
        assert stamp.endswith("Z")
        assert len(hexpart) == 6
        int(hexpart, 16)  # valid hex


# --------------------------------------------------------- resolve_within


def test_resolve_within_accepts_normal_parts(tmp_path: Path) -> None:
    p = resolve_within(tmp_path, "runs", "abc", "repository")
    assert p == (tmp_path / "runs" / "abc" / "repository").resolve()


def test_resolve_within_rejects_dotdot(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceEscapeError):
        resolve_within(tmp_path, "..", "etc")


def test_resolve_within_rejects_dotdot_inside(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceEscapeError):
        resolve_within(tmp_path, "runs", "..", "..", "escape")


def test_resolve_within_rejects_absolute(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceEscapeError):
        resolve_within(tmp_path, str(tmp_path.parent / "outside"))


@pytest.mark.skipif(sys.platform != "win32", reason="drive-switch case is Windows-only")
def test_resolve_within_rejects_drive_switch(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceEscapeError):
        resolve_within(tmp_path, "C:\\Windows\\System32")


def test_resolve_within_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks on this system: {exc}")
    with pytest.raises(WorkspaceEscapeError):
        resolve_within(root, "link", "payload")


def test_resolve_within_allows_symlink_inside(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    try:
        os.symlink(root / "real", root / "alias")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks on this system: {exc}")
    p = resolve_within(root, "alias", "file.txt")
    assert is_within(root, p)


# ---------------------------------------------------- validate_workspace


def test_validate_fails_when_repo_missing(tmp_path: Path) -> None:
    ws = manager.create_run(REF, _config(tmp_path))
    result = manager.validate_workspace(ws)
    assert not result.ok
    assert any("repository" in p for p in result.problems)


def test_validate_passes_after_local_clone(
    tmp_path: Path, bare_repo: Path, monkeypatch
) -> None:
    cfg = _config(tmp_path / "root")
    ws = manager.create_run(REF, cfg)
    monkeypatch.setattr(
        manager,
        "validate_clone_url",
        lambda url: str(bare_repo),
    )
    monkeypatch.setattr(
        manager,
        "_check_repo_size",
        lambda issue, config, client: None,
    )
    data = manager.clone_repository(
        ws, cfg, test_allow_local_path=True
    )
    result = manager.validate_workspace(ws, cfg)
    assert result.ok, result.problems
    assert data["base_branch"] == "main"
    assert len(data["base_commit"]) == 40
    assert data["work_branch"].startswith("swe-agent/issue-7-")
    assert result.head_branch == data["work_branch"]
    # status flips only via finalize_run
    import json

    raw = json.loads(ws.run_json_path.read_text(encoding="utf-8"))
    assert raw["status"] == "creating"
    manager.finalize_run(ws)
    raw = json.loads(ws.run_json_path.read_text(encoding="utf-8"))
    assert raw["status"] == "workspace_ready"


def test_validate_fails_on_dirty_tree(
    tmp_path: Path, bare_repo: Path, monkeypatch
) -> None:
    cfg = _config(tmp_path / "root")
    ws = manager.create_run(REF, cfg)
    monkeypatch.setattr(manager, "validate_clone_url", lambda url: str(bare_repo))
    monkeypatch.setattr(manager, "_check_repo_size", lambda *a: None)
    manager.clone_repository(ws, cfg, test_allow_local_path=True)
    (ws.repository / "dirty.txt").write_text("x", encoding="utf-8")
    result = manager.validate_workspace(ws, cfg)
    assert not result.ok
    assert any("clean" in p for p in result.problems)


def test_validate_fails_on_wrong_branch(
    tmp_path: Path, bare_repo: Path, monkeypatch
) -> None:
    cfg = _config(tmp_path / "root")
    ws = manager.create_run(REF, cfg)
    monkeypatch.setattr(manager, "validate_clone_url", lambda url: str(bare_repo))
    monkeypatch.setattr(manager, "_check_repo_size", lambda *a: None)
    manager.clone_repository(ws, cfg, test_allow_local_path=True)
    from swe_agent.tools import git_runner

    git_runner.run_git(
        ["switch", "main"], cwd=ws.repository, global_git_config=ws.state / "gitconfig"
    )
    result = manager.validate_workspace(ws, cfg)
    assert not result.ok
    assert any("HEAD" in p for p in result.problems)


# ------------------------------------------------------------------ clone


def test_clone_rejects_non_github_url(tmp_path: Path) -> None:
    for bad in (
        "file:///etc/passwd",
        "git@github.com:o/r.git",
        "ext::sh -c evil",
        "https://gitlab.com/o/r.git",
        "https://evil.test/o/r.git",
    ):
        with pytest.raises(manager.CloneError):
            manager.validate_clone_url(bad)


def test_clone_url_canonicalized(tmp_path: Path) -> None:
    assert (
        manager.validate_clone_url("https://github.com/o/r")
        == "https://github.com/o/r.git"
    )
    assert (
        manager.validate_clone_url("https://github.com/o/r.git")
        == "https://github.com/o/r.git"
    )


def test_failed_clone_leaves_no_directory(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path / "root")
    ws = manager.create_run(REF, cfg)
    # Point the clone at a nonexistent local path (test flag allows file://).
    monkeypatch.setattr(
        manager, "validate_clone_url", lambda url: str(tmp_path / "nope.git")
    )
    from swe_agent.workspace.errors import CloneError

    with pytest.raises(CloneError):
        manager.clone_repository(ws, cfg, test_allow_local_path=True)
    assert not ws.run_dir.exists(), "failed clone must remove the run directory"


def test_keyboard_interrupt_clone_cleans_up(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path / "root")
    ws = manager.create_run(REF, cfg)

    def boom(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(manager, "_check_repo_size", boom)
    with pytest.raises(KeyboardInterrupt):
        manager.clone_repository(ws, cfg)
    assert not ws.run_dir.exists()


def test_size_lookup_failure_warns_and_proceeds(
    tmp_path: Path, bare_repo: Path, monkeypatch
) -> None:
    from swe_agent.github.errors import NetworkError

    cfg = _config(tmp_path / "root")
    ws = manager.create_run(REF, cfg)
    monkeypatch.setattr(manager, "validate_clone_url", lambda url: str(bare_repo))

    def failing_size(issue, config, client):
        raise NetworkError("rate limited")

    # _check_repo_size catches internally; emulate by patching the client path
    class _Client:
        def get_repo_size_mb(self, owner, repo):
            raise NetworkError("rate limited")

    monkeypatch.setenv("GITHUB_TOKEN", "")
    data = manager.clone_repository(
        ws, cfg, github_client=_Client(), test_allow_local_path=True
    )
    assert data["base_branch"] == "main"


def test_repo_too_large_refuses_before_clone(tmp_path: Path, monkeypatch) -> None:
    from swe_agent.workspace.errors import RepoTooLargeError

    cfg = _config(tmp_path / "root")
    ws = manager.create_run(REF, cfg)

    class _Client:
        def get_repo_size_mb(self, owner, repo):
            return 9999.0

    with pytest.raises(RepoTooLargeError):
        manager.clone_repository(ws, cfg, github_client=_Client())
    assert not ws.run_dir.exists()


# ---------------------------------------------------------------- cleanup


def test_cleanup_removes_run(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    ws = manager.create_run(REF, cfg)
    manager.cleanup_run(ws.run_id, cfg)
    assert not ws.run_dir.exists()


def test_cleanup_refuses_unknown_run(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    manager.create_run(REF, cfg)
    with pytest.raises(CleanupRefusedError):
        manager.cleanup_run("20260101T000000Z-000000", cfg)


def test_cleanup_refuses_traversal(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    manager.create_run(REF, cfg)
    with pytest.raises(CleanupRefusedError):
        manager.cleanup_run("../..", cfg)
    with pytest.raises(CleanupRefusedError):
        manager.cleanup_run(str(tmp_path), cfg)


def test_cleanup_refuses_symlink(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    ws = manager.create_run(REF, cfg)
    victim = tmp_path / "victim"
    victim.mkdir()
    runs_root = tmp_path / "runs"
    link = runs_root / "sneaky"
    try:
        os.symlink(victim, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks on this system: {exc}")
    with pytest.raises(CleanupRefusedError):
        manager.cleanup_run("sneaky", cfg)
    assert victim.exists(), "symlink target must not be deleted"


def test_cleanup_refuses_missing(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(CleanupRefusedError):
        manager.cleanup_run("nope", cfg)


def test_list_runs(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert manager.list_runs(cfg) == []
    a = manager.create_run(REF, cfg)
    b = manager.create_run(REF, cfg)
    assert sorted([a.run_id, b.run_id]) == manager.list_runs(cfg)
