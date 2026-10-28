"""Run workspace lifecycle: create, safe clone, validate, cleanup.

Layout (root from config workspace.root, default ~/.swe-agent):

    <root>/runs/<run-id>/{repository/, state/, logs/, traces/, artifacts/}

No code from the cloned repository is ever executed here (or anywhere).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from swe_agent.config.schema import SweAgentConfig
from swe_agent.github.client import GitHubClient
from swe_agent.github.errors import GitHubError
from swe_agent.github.urls import IssueRef
from swe_agent.tools import git_runner
from swe_agent.tools.errors import GitExecutionError, GitTimeoutError, PolicyError
from swe_agent.workspace.errors import (
    CleanupRefusedError,
    CloneError,
    RepoTooLargeError,
    WorkspaceError,
    WorkspaceEscapeError,
    WorkspaceValidationError,
)
from swe_agent.workspace.paths import is_within, resolve_within

_CLONE_TIMEOUT_FALLBACK = 120


class RunWorkspace(BaseModel):
    """Filesystem layout of one run."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_id: str
    run_dir: Path
    repository: Path
    state: Path
    logs: Path
    traces: Path
    artifacts: Path

    @property
    def run_json_path(self) -> Path:
        return self.state / "run.json"


class WorkspaceValidation(BaseModel):
    """Structured outcome of validate_workspace."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    problems: list[str] = Field(default_factory=list)
    head_branch: str | None = None
    head_commit: str | None = None


def _new_run_id() -> str:
    """Short, sortable, collision-safe: UTC timestamp + 6 random hex chars."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically: temp file in the same dir, then os.replace."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_run_json(workspace: RunWorkspace) -> dict:
    return json.loads(workspace.run_json_path.read_text(encoding="utf-8"))


def create_run(issue_ref: IssueRef, config: SweAgentConfig) -> RunWorkspace:
    """Create the isolated run directory tree and initial run.json.

    run.json starts with status="creating"; it becomes "workspace_ready"
    only after clone AND validation succeed.
    """
    runs_root = resolve_within(config.workspace.root, "runs")
    run_id = _new_run_id()
    run_dir = resolve_within(runs_root, run_id)
    ws = RunWorkspace(
        run_id=run_id,
        run_dir=run_dir,
        repository=run_dir / "repository",
        state=run_dir / "state",
        logs=run_dir / "logs",
        traces=run_dir / "traces",
        artifacts=run_dir / "artifacts",
    )
    for path in (ws.repository, ws.state, ws.logs, ws.traces, ws.artifacts):
        path.mkdir(parents=True, exist_ok=False)
    # Empty hooks dir: safe clone points core.hooksPath here so repository
    # hooks can never run.
    (ws.state / "hooks").mkdir()
    # Empty global git config, scrubbed into every git invocation.
    (ws.state / "gitconfig").write_text("", encoding="utf-8")

    clone_url = f"https://github.com/{issue_ref.owner}/{issue_ref.repo}.git"
    _atomic_write_json(
        ws.run_json_path,
        {
            "run_id": run_id,
            "issue": {
                "owner": issue_ref.owner,
                "repo": issue_ref.repo,
                "number": issue_ref.number,
            },
            "clone_url": clone_url,
            "base_branch": None,
            "base_commit": None,
            "work_branch": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "creating",
        },
    )
    return ws


def validate_clone_url(url: str) -> str:
    """Accept only https://github.com/{owner}/{repo}[.git]; return canonical .git URL."""
    prefix = "https://github.com/"
    if not url.startswith(prefix):
        raise CloneError(
            f"clone URL must start with {prefix} (got {url.split(':', 1)[0]}:...)"
        )
    rest = url[len(prefix) :]
    if rest.endswith(".git"):
        rest = rest[: -len(".git")]
    if rest.count("/") != 1:
        raise CloneError(f"clone URL must be owner/repo (got {url!r})")
    owner, repo = rest.split("/")
    from swe_agent.github.urls import _OWNER_RE, _REPO_RE  # reuse charset rules

    if not _OWNER_RE.match(owner) or not _REPO_RE.match(repo) or repo in (".", ".."):
        raise CloneError(f"invalid owner/repo in clone URL: {url!r}")
    return f"{prefix}{owner}/{repo}.git"


def clone_repository(
    workspace: RunWorkspace,
    config: SweAgentConfig,
    *,
    github_client: GitHubClient | None = None,
    test_allow_local_path: bool = False,
    test_extra_env: dict[str, str] | None = None,
) -> dict:
    """Shallow-clone the target repo, record base branch/SHA, create the work branch.

    test_allow_local_path=True is TEST-ONLY: it permits file-protocol clones
    of local fixture repos by setting protocol.file.allow=always.
    test_extra_env is TEST-ONLY: extra env for git (e.g. relaxing
    GIT_ALLOW_PROTOCOL for local fixtures). Never set either in production
    code paths.

    Any failure (including KeyboardInterrupt) removes the partially created
    run directory before re-raising.
    """
    run_data = _read_run_json(workspace)
    issue = run_data["issue"]
    work_branch = f"swe-agent/issue-{issue['number']}-{workspace.run_id}"

    try:
        clone_url = validate_clone_url(run_data["clone_url"])
        _check_repo_size(issue, config, github_client)

        git_config = [
            f"core.hooksPath={workspace.state / 'hooks'}",
            "core.fsmonitor=false",
            "protocol.ext.allow=never",
            # file:// only for local test fixtures; production is https-only.
            "protocol.file.allow=always" if test_allow_local_path else "protocol.file.allow=never",
        ]
        timeout = getattr(config.workspace, "clone_timeout_seconds", _CLONE_TIMEOUT_FALLBACK)
        clone_env = dict(test_extra_env or {})
        if test_allow_local_path:
            # Local fixture transport must be explicitly allowed on top of
            # the scrubbed GIT_ALLOW_PROTOCOL=https.
            clone_env.setdefault("GIT_ALLOW_PROTOCOL", "https:file")
        result = git_runner.run_git(
            [
                "clone",
                "--depth=1",
                "--no-recurse-submodules",
                "--no-tags",
                clone_url,
                "repository",
            ],
            cwd=workspace.run_dir,
            timeout=timeout,
            config_overrides=git_config,
            extra_env=clone_env or None,
            global_git_config=workspace.state / "gitconfig",
        )
        if result.exit_code != 0:
            raise CloneError(
                f"git clone failed (exit {result.exit_code}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

        base = _git_ok(
            workspace, ["rev-parse", "--abbrev-ref", "HEAD"], "resolve default branch"
        )
        base_branch = base.stdout.strip()
        commit = _git_ok(workspace, ["rev-parse", "HEAD"], "resolve HEAD commit")
        base_commit = commit.stdout.strip()

        switch = git_runner.run_git(
            ["switch", "-c", work_branch],
            cwd=workspace.repository,
            timeout=30,
            global_git_config=workspace.state / "gitconfig",
        )
        if switch.exit_code != 0:
            raise CloneError(
                f"failed to create work branch {work_branch!r}: {switch.stderr.strip()}"
            )

        run_data.update(
            base_branch=base_branch,
            base_commit=base_commit,
            work_branch=work_branch,
        )
        _atomic_write_json(workspace.run_json_path, run_data)
        return run_data

    except BaseException as exc:
        # Failed or interrupted clone must leave no half-created run dir.
        _force_rmtree(workspace.run_dir)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, (CloneError, RepoTooLargeError, WorkspaceError)):
            raise
        raise CloneError(f"clone failed: {exc}") from exc


def _git_ok(workspace: RunWorkspace, args: list[str], what: str):
    try:
        result = git_runner.run_git(
            args,
            cwd=workspace.repository,
            timeout=30,
            global_git_config=workspace.state / "gitconfig",
        )
    except (GitTimeoutError, GitExecutionError, PolicyError) as exc:
        raise CloneError(f"failed to {what}: {exc}") from exc
    if result.exit_code != 0:
        raise CloneError(f"failed to {what}: {result.stderr.strip()}")
    return result


def _check_repo_size(
    issue: dict,
    config: SweAgentConfig,
    client: GitHubClient | None,
) -> None:
    """Refuse to clone repos over the configured limit; lookup failure warns and proceeds."""
    import logging

    log = logging.getLogger(__name__)
    if client is None:
        client = GitHubClient(token=os.environ.get("GITHUB_TOKEN") or None)
    try:
        size_mb = client.get_repo_size_mb(issue["owner"], issue["repo"])
    except GitHubError as exc:
        log.warning("could not determine repo size (%s); proceeding", exc)
        return
    except Exception as exc:  # defensive: never block the clone on size lookup
        log.warning("could not determine repo size (%s); proceeding", exc)
        return
    limit = config.workspace.max_repo_size_mb
    if size_mb > limit:
        raise RepoTooLargeError(
            f"repository {issue['owner']}/{issue['repo']} is {size_mb:.0f}MB, "
            f"over the {limit}MB limit"
        )


def validate_workspace(
    workspace: RunWorkspace,
    config: SweAgentConfig | None = None,
) -> WorkspaceValidation:
    """Verify the cloned workspace. Callers set status=workspace_ready only if ok."""
    problems: list[str] = []

    # repository exists and is inside the run dir
    try:
        resolve_within(workspace.run_dir, "repository")
    except WorkspaceEscapeError as exc:
        problems.append(f"repository path escapes run dir: {exc}")
    if not workspace.repository.is_dir():
        problems.append("repository directory missing")
        return WorkspaceValidation(ok=False, problems=problems)

    # is a git repo
    try:
        git_dir = git_runner.run_git(
            ["rev-parse", "--git-dir"],
            cwd=workspace.repository,
            timeout=30,
            global_git_config=workspace.state / "gitconfig",
        )
    except (GitTimeoutError, GitExecutionError, PolicyError) as exc:
        problems.append(f"git check failed: {exc}")
        return WorkspaceValidation(ok=False, problems=problems)
    if git_dir.exit_code != 0:
        problems.append("repository is not a git repository")
        return WorkspaceValidation(ok=False, problems=problems)

    run_data = _read_run_json(workspace)
    work_branch = run_data.get("work_branch")

    # HEAD on the work branch
    head = git_runner.run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=workspace.repository,
        timeout=30,
        global_git_config=workspace.state / "gitconfig",
    )
    head_branch = head.stdout.strip() if head.exit_code == 0 else None
    if head_branch != work_branch:
        problems.append(
            f"HEAD is on {head_branch!r}, expected work branch {work_branch!r}"
        )

    head_commit = None
    sha = git_runner.run_git(
        ["rev-parse", "HEAD"],
        cwd=workspace.repository,
        timeout=30,
        global_git_config=workspace.state / "gitconfig",
    )
    if sha.exit_code == 0:
        head_commit = sha.stdout.strip()

    # working tree clean
    status = git_runner.run_git(
        ["status", "--porcelain"],
        cwd=workspace.repository,
        timeout=30,
        global_git_config=workspace.state / "gitconfig",
    )
    if status.exit_code != 0:
        problems.append("git status failed")
    elif status.stdout.strip():
        problems.append("working tree is not clean")

    # no symlink in the run root pointing outside
    for entry in workspace.run_dir.iterdir():
        if entry.is_symlink():
            target = entry.resolve()
            if not is_within(workspace.run_dir, target):
                problems.append(f"symlink in run root escapes workspace: {entry.name}")
        elif sys.version_info >= (3, 12) and _is_junction(entry):
            target = entry.resolve()
            if not is_within(workspace.run_dir, target):
                problems.append(f"junction in run root escapes workspace: {entry.name}")

    return WorkspaceValidation(
        ok=not problems,
        problems=problems,
        head_branch=head_branch,
        head_commit=head_commit,
    )


def _is_junction(path: Path) -> bool:
    if sys.version_info >= (3, 12) and hasattr(path, "is_junction"):
        try:
            return path.is_junction()
        except OSError:
            return False
    return False


def finalize_run(workspace: RunWorkspace) -> dict:
    """Mark the run workspace_ready in run.json (call only after validation)."""
    data = _read_run_json(workspace)
    data["status"] = "workspace_ready"
    _atomic_write_json(workspace.run_json_path, data)
    return data


def cleanup_run(run_id: str, config: SweAgentConfig) -> None:
    """Delete <root>/runs/<run-id>. Refuses paths outside runs/ and symlinks."""
    runs_root = resolve_within(config.workspace.root, "runs")
    try:
        target = resolve_within(runs_root, run_id)
    except WorkspaceEscapeError as exc:
        raise CleanupRefusedError(f"refusing to clean {run_id!r}: {exc}") from exc
    if target == runs_root or runs_root not in target.parents:
        raise CleanupRefusedError(
            f"refusing to clean {run_id!r}: not strictly inside {runs_root}"
        )
    if not target.exists():
        raise CleanupRefusedError(f"run {run_id!r} does not exist")
    if target.is_symlink() or _is_junction(target):
        raise CleanupRefusedError(f"refusing to clean {run_id!r}: is a symlink/junction")
    if not target.is_dir():
        raise CleanupRefusedError(f"refusing to clean {run_id!r}: not a directory")
    _force_rmtree(target)


def list_runs(config: SweAgentConfig) -> list[str]:
    """Run ids under <root>/runs/ (empty list if the root doesn't exist)."""
    runs_root = resolve_within(config.workspace.root, "runs")
    if not runs_root.is_dir():
        return []
    return sorted(p.name for p in runs_root.iterdir() if p.is_dir() and not p.is_symlink())


def _force_rmtree(path: Path) -> None:
    """rmtree that clears Windows read-only bits (common in .git)."""

    def _onexc(func, p, exc):  # Python 3.12+ signature
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except FileNotFoundError:
            pass
        except OSError:
            raise exc

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_onexc)
    else:  # pragma: no cover - dev on 3.12

        def _onerror(func, p, exc):
            _onexc(func, p, exc)

        shutil.rmtree(path, onerror=_onerror)
