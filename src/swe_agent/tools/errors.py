"""Typed errors for tool execution (git runner, future tools)."""

from __future__ import annotations


class ToolError(Exception):
    """Base class for tool-layer errors."""


class PolicyError(ToolError):
    """A command or argument is not permitted by the tool policy."""


class GitTimeoutError(ToolError):
    """A git process exceeded its timeout and was killed (including its tree)."""


class GitExecutionError(ToolError):
    """A git process could not be started or failed at the OS level."""
