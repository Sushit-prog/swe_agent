"""Path containment for the run workspace. All workspace file ops go through this."""

from __future__ import annotations

from pathlib import Path

from swe_agent.workspace.errors import WorkspaceEscapeError


def resolve_within(root: Path | str, *parts: str) -> Path:
    """Join `parts` onto `root` and return the resolved path.

    Rejects anything that resolves outside `root`:
    - `..` traversal segments
    - absolute parts (including Windows drive switches like ``C:\\evil``)
    - symlinks/junctions anywhere along the path that lead outside `root`

    The containment check uses fully resolved paths (symlinks followed), so
    a symlink inside root pointing outside is rejected even if the textual
    path looks safe.
    """
    root_path = Path(root).expanduser().resolve()
    candidate = root_path
    for part in parts:
        piece = Path(part)
        if piece.is_absolute() or piece.drive or piece.root:
            raise WorkspaceEscapeError(
                f"absolute path component not allowed: {part!r}"
            )
        candidate = candidate / piece
    resolved = candidate.resolve()
    if resolved != root_path and root_path not in resolved.parents:
        raise WorkspaceEscapeError(
            f"path escapes workspace root: {resolved} is not within {root_path}"
        )
    return resolved


def is_within(root: Path | str, candidate: Path | str) -> bool:
    """True if `candidate` resolves to a location inside `root` (or is root)."""
    root_path = Path(root).expanduser().resolve()
    cand_path = Path(candidate).expanduser().resolve()
    return cand_path == root_path or root_path in cand_path.parents
