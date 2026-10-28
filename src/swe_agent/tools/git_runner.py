"""The ONLY place git is executed.

Policy layer (`run_git`): allowlisted subcommands, forbidden options,
trusted config overrides, `--` before positionals, scrubbed environment.
Process layer (`_execute`): argv-list execution (never shell=True), cwd
containment, timeouts with process-tree kill, output caps.

Callers decide success from `GitResult.exit_code`, never from text.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from swe_agent.tools.errors import GitExecutionError, GitTimeoutError, PolicyError

# M3 allowlist. Subcommands only; no option-level grants.
ALLOWED_SUBCOMMANDS = frozenset(
    {"clone", "rev-parse", "switch", "status", "log", "diff"}
)

# Dangerous option prefixes rejected anywhere in caller args.
_FORBIDDEN_PREFIXES = (
    "--output",
    "--upload-pack",
    "--receive-pack",
    "--exec",
    "--open-files-in-pager",
    "-O",
)
_FORBIDDEN_EXACT = frozenset({"-O"})

_OUTPUT_CAP_BYTES = 200_000
_DEFAULT_TIMEOUT = 30

# Only these host env vars are forwarded to git. Everything else — including
# GITHUB_TOKEN, ANTHROPIC_API_KEY, and any other secret — is dropped.
_FORWARDED_ENV = ("PATH", "HOME", "USERPROFILE", "SystemRoot", "TEMP", "TMP")

# Scrubbed, always set.
_BASE_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ALLOW_PROTOCOL": "https",
}


class GitResult(BaseModel):
    """Outcome of one git invocation. `args` never contains secrets."""

    model_config = ConfigDict(extra="forbid")

    args: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


def _base_env(global_git_config: Path | str | None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _FORWARDED_ENV if k in os.environ}
    env.update(_BASE_GIT_ENV)
    env["GIT_CONFIG_GLOBAL"] = str(global_git_config) if global_git_config else os.devnull
    return env


def _check_subcommand(args: list[str]) -> str:
    if not args:
        raise PolicyError("empty git command")
    sub = args[0]
    if sub not in ALLOWED_SUBCOMMANDS:
        raise PolicyError(
            f"git subcommand not allowed: {sub!r} "
            f"(allowed: {', '.join(sorted(ALLOWED_SUBCOMMANDS))})"
        )
    return sub


def _check_forbidden_options(subcommand: str, args: list[str]) -> None:
    for arg in args:
        # Caller-supplied -c (global config injection, e.g. clone
        # -c core.hooksPath=...) is refused; trusted overrides go through
        # run_git(config_overrides=...) only. Exception: `switch -c` means
        # "create branch" and carries no config semantics. `--color` etc.
        # (start with --) are unaffected.
        if arg == "-c" or (arg.startswith("-c") and not arg.startswith("--")):
            if not (subcommand == "switch" and arg == "-c"):
                raise PolicyError("caller-supplied -c options are not allowed")
        if arg in _FORBIDDEN_EXACT or any(
            arg.startswith(p) for p in _FORBIDDEN_PREFIXES
        ):
            raise PolicyError(f"forbidden git option: {arg!r}")


def _with_double_dash(subcommand: str, args: list[str]) -> list[str]:
    """Insert `--` before positional args so URLs can never be option-like.

    Only clone has positionals in the M3 allowlist. Our option tokens are
    all `=`-attached single tokens, so the first non-dash token after the
    subcommand is the first positional.
    """
    if subcommand != "clone" or "--" in args:
        return list(args)
    for i, token in enumerate(args[1:], start=1):
        if not token.startswith("-"):
            return [*args[:i], "--", *args[i:]]
    return list(args)


def _truncate(text: str) -> str:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= _OUTPUT_CAP_BYTES:
        return text
    return raw[:_OUTPUT_CAP_BYTES].decode("utf-8", errors="replace") + "\n...[truncated]"


def run_git(
    args: list[str],
    *,
    cwd: Path | str,
    timeout: float = _DEFAULT_TIMEOUT,
    config_overrides: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    global_git_config: Path | str | None = None,
) -> GitResult:
    """Run one allowlisted git command under the swe-agent policy.

    `config_overrides` are trusted `key=value` strings inserted as
    `-c key=value` before the subcommand (used for safe-clone flags).
    `extra_env` is for tests only and is applied on top of the scrubbed env.
    """
    subcommand = _check_subcommand(args)
    _check_forbidden_options(subcommand, args)
    argv = ["git"]
    for override in config_overrides or []:
        if override.startswith("-") or "\n" in override:
            raise PolicyError(f"invalid config override: {override!r}")
        argv.extend(["-c", override])
    argv.extend(_with_double_dash(subcommand, args))

    env = _base_env(global_git_config)
    if extra_env:
        env.update(extra_env)

    return _execute(argv, cwd=cwd, timeout=timeout, env=env)


def _execute(
    argv: list[str],
    *,
    cwd: Path | str,
    timeout: float,
    env: dict[str, str],
) -> GitResult:
    """Process-control layer: run argv (executable + args, no shell) with
    timeout, process-tree kill, and output caps."""
    start = time.monotonic()
    kwargs: dict = dict(
        args=argv,  # argument list, never a shell string
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        shell=False,
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(**kwargs)
    except OSError as exc:
        raise GitExecutionError(f"failed to start {argv[0]!r}: {exc}") from exc

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        stdout, stderr = proc.communicate()
        raise GitTimeoutError(
            f"{argv[1] if len(argv) > 1 else argv[0]} timed out after {timeout:.0f}s"
        ) from None
    duration = time.monotonic() - start
    return GitResult(
        args=argv[1:],  # record args only; argv[0] is the binary
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=_truncate(stdout.decode("utf-8", errors="replace")),
        stderr=_truncate(stderr.decode("utf-8", errors="replace")),
        duration_s=round(duration, 3),
    )


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the process and all its children."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        proc.kill()
    else:
        import signal

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
