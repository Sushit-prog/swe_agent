"""Typed errors for workspace operations."""

from __future__ import annotations


class WorkspaceError(Exception):
    """Base class for all swe-agent workspace errors."""


class WorkspaceEscapeError(WorkspaceError):
    """A path resolved outside its allowed root (traversal, absolute, symlink)."""


class CloneError(WorkspaceError):
    """Repository clone failed."""


class RepoTooLargeError(WorkspaceError):
    """Repository exceeds the configured size limit."""


class WorkspaceValidationError(WorkspaceError):
    """The run workspace failed post-clone validation."""


class CleanupRefusedError(WorkspaceError):
    """Cleanup target is missing, outside <root>/runs/, or a symlink."""
