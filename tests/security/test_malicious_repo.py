"""Malicious-repo defenses: hooks never run, submodules never fetched.

Amendment: the hook test includes a CONTROL clone without the safe flags —
proving the marker hook CAN fire — then asserts it does not fire with the
safe clone flags. Hooks come from init.templateDir via GIT_CONFIG_GLOBAL
(they cannot be committed inside a cloned repo).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from swe_agent.config.schema import SweAgentConfig
from swe_agent.github.urls import IssueRef
from swe_agent.workspace import manager
from swe_agent.workspace.errors import CloneError

REF = IssueRef(owner="o", repo="r", number=7)


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


def _build_malicious_repo(path: Path) -> Path:
    """Repo containing a .gitmodules entry pointing at a hostile URL."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "evil@example.com"], path)
    _git(["config", "user.name", "Evil"], path)
    (path / "readme.md").write_text("innocent\n", encoding="utf-8")
    (path / ".gitmodules").write_text(
        '[submodule "evil"]\n\tpath = evil\n\turl = ext::sh -c "touch pwned"\n',
        encoding="utf-8",
    )
    _git(["add", "-A"], path)
    _git(["commit", "-m", "init"], path)
    # Add a real gitlink so --no-recurse-submodules has something to skip.
    sha = _git(["rev-parse", "HEAD"], path).strip()
    _git(["update-index", "--add", "--cacheinfo", f"160000,{sha},evil"], path)
    _git(["commit", "-m", "add submodule"], path)
    return path


def _build_template_with_post_checkout(template: Path, marker: Path) -> Path:
    """init.templateDir containing a post-checkout hook that writes `marker`."""
    hooks = template / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "post-checkout"
    hook.write_text(
        f'#!/bin/sh\necho fired > "{marker.as_posix()}"\n',
        encoding="utf-8",
    )
    return template


def _global_config_with_template(cfg_path: Path, template: Path) -> Path:
    cfg_path.write_text(
        f"[init]\n\ttemplateDir = {template.as_posix()}\n", encoding="utf-8"
    )
    return cfg_path


@pytest.fixture
def hostile_env(tmp_path: Path) -> dict[str, str]:
    """Env with a global config whose template installs a marker-writing hook."""
    template = _build_template_with_post_checkout(
        tmp_path / "template", tmp_path / "hook-marker.txt"
    )
    cfg = _global_config_with_template(tmp_path / "gitconfig", template)
    return {
        "GIT_CONFIG_GLOBAL": cfg.as_posix(),
        "GIT_ALLOW_PROTOCOL": "https:file",
    }


def test_control_clone_without_safe_flags_fires_hook(
    tmp_path: Path, hostile_env: dict[str, str]
) -> None:
    """CONTROL: without hooksPath override, the template hook runs on clone."""
    from swe_agent.tools import git_runner

    repo = _build_malicious_repo(tmp_path / "evil-src")
    marker = tmp_path / "hook-marker.txt"
    result = git_runner.run_git(
        ["clone", "--no-tags", str(repo), "control-out"],
        cwd=tmp_path,
        timeout=60,
        extra_env=hostile_env,
    )
    assert result.exit_code == 0, result.stderr
    assert marker.exists(), (
        "control clone should fire the post-checkout hook — "
        "if this fails, the hook mechanism doesn't work on this system "
        "and the safe-clone assertion below proves nothing"
    )


def test_safe_clone_does_not_fire_hook(
    tmp_path: Path, hostile_env: dict[str, str]
) -> None:
    """With the safe flags (empty hooksPath), the marker hook must NOT run."""
    from swe_agent.tools import git_runner

    repo = _build_malicious_repo(tmp_path / "evil-src")
    marker = tmp_path / "hook-marker.txt"
    state = tmp_path / "state"
    state.mkdir()
    empty_hooks = state / "hooks"
    empty_hooks.mkdir()
    (state / "gitconfig").write_text("", encoding="utf-8")

    result = git_runner.run_git(
        ["clone", "--depth=1", "--no-recurse-submodules", "--no-tags",
         str(repo), "safe-out"],
        cwd=tmp_path,
        timeout=60,
        config_overrides=[
            f"core.hooksPath={empty_hooks}",
            "core.fsmonitor=false",
            "protocol.ext.allow=never",
            "protocol.file.allow=always",
        ],
        extra_env=hostile_env,
        global_git_config=state / "gitconfig",
    )
    assert result.exit_code == 0, result.stderr
    assert not marker.exists(), "safe clone must not run template hooks"


def test_safe_clone_does_not_fetch_submodules(
    tmp_path: Path, hostile_env: dict[str, str]
) -> None:
    from swe_agent.tools import git_runner

    repo = _build_malicious_repo(tmp_path / "evil-src")
    state = tmp_path / "state"
    state.mkdir()
    empty_hooks = state / "hooks"
    empty_hooks.mkdir()
    (state / "gitconfig").write_text("", encoding="utf-8")

    out = tmp_path / "sub-out"
    result = git_runner.run_git(
        ["clone", "--depth=1", "--no-recurse-submodules", "--no-tags",
         str(repo), str(out)],
        cwd=tmp_path,
        timeout=60,
        config_overrides=[
            f"core.hooksPath={empty_hooks}",
            "protocol.ext.allow=never",
            "protocol.file.allow=always",
        ],
        extra_env=hostile_env,
        global_git_config=state / "gitconfig",
    )
    assert result.exit_code == 0, result.stderr
    # gitlink placeholder dir may exist but must be EMPTY (no content fetched),
    # and the ext:: URL must never have been contacted.
    evil = out / "evil"
    if evil.exists():
        assert not any(evil.iterdir()), "submodule content must not be fetched"
        assert not (evil / ".git").exists()
    assert not (tmp_path / "pwned").exists()
    # .gitmodules may exist as data; it must not have been acted on
    status = git_runner.run_git(
        ["status", "--porcelain"], cwd=out, timeout=30,
        global_git_config=state / "gitconfig",
    )
    assert status.stdout.strip() == ""


def test_ext_protocol_blocked_by_safe_flags(tmp_path: Path) -> None:
    """protocol.ext.allow=never rejects ext:: URLs outright."""
    from swe_agent.tools import git_runner

    state = tmp_path / "state"
    state.mkdir()
    (state / "hooks").mkdir()
    (state / "gitconfig").write_text("", encoding="utf-8")
    result = git_runner.run_git(
        ["clone", "ext::sh -c evil", "out"],
        cwd=tmp_path,
        timeout=15,
        config_overrides=["protocol.ext.allow=never"],
        extra_env={"GIT_ALLOW_PROTOCOL": "https:ext:file"},
        global_git_config=state / "gitconfig",
    )
    assert result.exit_code != 0
    assert not (tmp_path / "out" / "evil").exists()


# ------------------------------------------------- URL validation (M3)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "file://localhost/etc/passwd",
        "git@github.com:o/r.git",
        "ssh://git@github.com/o/r.git",
        "ext::sh -c evil",
        "https://gitlab.com/o/r.git",
        "https://bitbucket.org/o/r.git",
        "https://github.com.evil.test/o/r.git",
        "https://evil.test/github.com/o/r.git",
        "http://github.com/o/r.git",
        "https://github.com/o",
        "https://github.com/o/r/extra.git",
        "https://github.com/../r.git",
    ],
)
def test_clone_url_validation_rejects(url: str) -> None:
    with pytest.raises(CloneError):
        manager.validate_clone_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r.git",
        "https://github.com/o/r",
        "https://github.com/my-org/my.repo.git",
    ],
)
def test_clone_url_validation_accepts(url: str) -> None:
    assert manager.validate_clone_url(url).startswith("https://github.com/")


def test_production_clone_never_allows_file_protocol(tmp_path: Path) -> None:
    """Without the test flag, protocol.file.allow must be never."""
    import inspect

    source = inspect.getsource(manager.clone_repository)
    assert "protocol.file.allow=never" in source
    assert 'if test_allow_local_path else "protocol.file.allow=never"' in source


def test_no_tokens_in_clone_url_or_config(tmp_path: Path, monkeypatch) -> None:
    """Clone URL comes from the issue ref only; tokens never embedded."""
    cfg = SweAgentConfig(workspace={"root": str(tmp_path / "root")})
    ws = manager.create_run(REF, cfg)
    import json

    data = json.loads(ws.run_json_path.read_text(encoding="utf-8"))
    assert data["clone_url"] == "https://github.com/o/r.git"
    assert "token" not in data["clone_url"].lower()
    # gitconfig written for the run is empty
    assert (ws.state / "gitconfig").read_text(encoding="utf-8") == ""
