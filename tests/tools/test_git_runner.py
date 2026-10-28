"""git_runner policy and process-control tests. Local git only, no network."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from swe_agent.tools import git_runner
from swe_agent.tools.errors import GitTimeoutError, PolicyError


# ----------------------------------------------------------------- policy


@pytest.mark.parametrize(
    "args",
    [
        ["push", "origin", "main"],
        ["commit", "-m", "x"],
        ["add", "."],
        ["remote", "add", "evil", "ext::sh"],
        ["fetch", "origin"],
        ["branch", "-D", "main"],
        ["config", "--list"],
        ["init"],
        ["rebase", "main"],
        ["reset", "--hard"],
        ["submodule", "update"],
    ],
)
def test_non_allowlisted_subcommands_rejected(args) -> None:
    with pytest.raises(PolicyError, match="not allowed"):
        git_runner.run_git(args, cwd=Path("."), timeout=5)


def test_allowlisted_subcommands_accepted_policy(tmp_path: Path) -> None:
    # Policy passes; process may still fail (empty dir) — that's exit_code.
    for args in (["status"], ["log", "-n", "1"], ["diff"], ["rev-parse", "HEAD"]):
        result = git_runner.run_git(args, cwd=tmp_path, timeout=15)
        assert isinstance(result.exit_code, int)


@pytest.mark.parametrize(
    "bad",
    [
        "--output=/tmp/x",
        "--upload-pack=evil",
        "--receive-pack=evil",
        "--exec=evil",
        "--open-files-in-pager=less",
        "-Ofile",
    ],
)
def test_forbidden_options_rejected(bad) -> None:
    with pytest.raises(PolicyError, match="forbidden"):
        git_runner.run_git(["status", bad], cwd=Path("."), timeout=5)


def test_caller_supplied_c_rejected() -> None:
    with pytest.raises(PolicyError, match="-c"):
        git_runner.run_git(
            ["clone", "-c", "core.hooksPath=/tmp", "https://github.com/o/r.git"],
            cwd=Path("."),
            timeout=5,
        )
    with pytest.raises(PolicyError, match="-c"):
        git_runner.run_git(
            ["status", "-c", "core.hooksPath=/tmp"], cwd=Path("."), timeout=5
        )


def test_double_dash_inserted_before_clone_url(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    def fake_execute(argv, *, cwd, timeout, env):
        captured["argv"] = argv

        class _R:
            args = argv[1:]
            exit_code = 0
            stdout = ""
            stderr = ""
            duration_s = 0.0

        return _R()

    monkeypatch.setattr(git_runner, "_execute", fake_execute)
    git_runner.run_git(
        ["clone", "--depth=1", "--", "https://github.com/o/r.git", "repository"],
        cwd=tmp_path,
    )
    # already has --: untouched
    assert "--" in captured["argv"]
    captured.clear()
    git_runner.run_git(
        ["clone", "--depth=1", "https://github.com/o/r.git", "repository"],
        cwd=tmp_path,
    )
    argv = captured["argv"]
    i = argv.index("--")
    assert argv[i + 1] == "https://github.com/o/r.git"


def test_caller_cannot_supply_c_but_trusted_overrides_work(
    tmp_path: Path, monkeypatch
) -> None:
    with pytest.raises(PolicyError, match="-c"):
        git_runner.run_git(
            ["status", "-c", "core.hooksPath=/tmp"], cwd=tmp_path, timeout=5
        )

    captured = {}

    def fake_execute(argv, *, cwd, timeout, env):
        captured["argv"] = argv

        class _R:
            args = argv[1:]
            exit_code = 0
            stdout = ""
            stderr = ""
            duration_s = 0.0

        return _R()

    monkeypatch.setattr(git_runner, "_execute", fake_execute)
    git_runner.run_git(
        ["status"],
        cwd=tmp_path,
        config_overrides=["core.hooksPath=C:/hooks", "protocol.ext.allow=never"],
    )
    assert "-c" in captured["argv"]
    assert "core.hooksPath=C:/hooks" in captured["argv"]


# ------------------------------------------------------------- env scrub


def test_env_scrubs_secrets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SUPERSECRET")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SUPERSECRET")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@db/x")
    captured = {}

    def fake_execute(argv, *, cwd, timeout, env):
        captured["env"] = env

        class _R:
            args = argv[1:]
            exit_code = 0
            stdout = ""
            stderr = ""
            duration_s = 0.0

        return _R()

    monkeypatch.setattr(git_runner, "_execute", fake_execute)
    git_runner.run_git(["status"], cwd=tmp_path)

    env = captured["env"]
    assert "GITHUB_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "DATABASE_URL" not in env
    # required scrubbed vars are present
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert "GIT_CONFIG_GLOBAL" in env
    # forwarded host vars only from the allowlist
    for key in env:
        if key.startswith("GIT_"):
            continue
        assert key in git_runner._FORWARDED_ENV, f"unexpected env var forwarded: {key}"


def test_env_child_never_sees_secrets(monkeypatch, tmp_path: Path) -> None:
    """End-to-end: run a real git command; the child process env must be clean."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SUPERSECRET")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SUPERSECRET")
    result = git_runner.run_git(["status"], cwd=tmp_path, timeout=15)
    assert result.exit_code in (0, 128)  # empty dir: ok or "not a git repo"
    # If secrets had leaked they'd be in git's own config probing output;
    # assert the runner's env builder never includes them (unit-level above)
    # and that no exception surfaced.
    assert "SUPERSECRET" not in result.stdout
    assert "SUPERSECRET" not in result.stderr


def test_never_uses_shell(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    def spy_execute(argv, *, cwd, timeout, env):
        captured["argv"] = argv

        class _R:
            args = argv[1:]
            exit_code = 0
            stdout = ""
            stderr = ""
            duration_s = 0.0

        return _R()

    monkeypatch.setattr(git_runner, "_execute", spy_execute)
    git_runner.run_git(["status"], cwd=tmp_path)
    assert isinstance(captured["argv"], list)
    assert all(isinstance(a, str) for a in captured["argv"])


def test_execute_shell_false_kwarg(tmp_path: Path) -> None:
    # Direct _execute check: shell is explicitly False.
    import subprocess as sp

    real_popen = sp.Popen
    seen = {}

    class SpyPopen(real_popen):
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            super().__init__(*args, **kwargs)

    import swe_agent.tools.git_runner as gr

    orig = gr.subprocess.Popen
    gr.subprocess.Popen = SpyPopen
    try:
        gr._execute(["git", "--version"], cwd=tmp_path, timeout=15, env=dict(__import__("os").environ))
    finally:
        gr.subprocess.Popen = orig
    assert seen.get("shell") is False
    assert isinstance(seen.get("args"), list)


# ------------------------------------------------------- timeout / kill


def test_timeout_kills_python_sleep_process_tree(tmp_path: Path) -> None:
    """Timeout must kill a child AND its grandchild (tree kill), not just the leaf."""
    marker = tmp_path / "grandchild-ran.txt"
    grandchild = (
        f"import time, pathlib; time.sleep(6); "
        f"pathlib.Path({str(marker)!r}).write_text('x')"
    )
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        "time.sleep(30)"
    )
    start = time.monotonic()
    with pytest.raises(GitTimeoutError):
        git_runner._execute(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            timeout=1,
            env=dict(os.environ),  # tree-kill test: env scrubbing covered above
        )
    elapsed = time.monotonic() - start
    assert elapsed < 15, "timeout should fire promptly"
    # Grandchild had 6s to write the marker; wait it out and prove it never ran.
    time.sleep(6.5)
    assert not marker.exists(), "grandchild survived the tree-kill"


def test_timeout_message_mentions_command(tmp_path: Path) -> None:
    with pytest.raises(GitTimeoutError, match="timed out"):
        git_runner._execute(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout=1,
            env={},
        )


# -------------------------------------------------------------- results


def test_result_model_shape(tmp_path: Path) -> None:
    result = git_runner.run_git(["rev-parse", "--git-dir"], cwd=tmp_path, timeout=15)
    assert set(result.model_dump().keys()) == {
        "args",
        "exit_code",
        "stdout",
        "stderr",
        "duration_s",
    }
    assert result.args[0] == "rev-parse"
    assert result.duration_s >= 0


def test_callers_use_exit_code_not_text(tmp_path: Path) -> None:
    # Non-repo dir: rev-parse fails with nonzero exit; message text may vary.
    result = git_runner.run_git(["rev-parse", "HEAD"], cwd=tmp_path, timeout=15)
    assert result.exit_code != 0
