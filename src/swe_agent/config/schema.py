"""Configuration schema and YAML loader.

Schema reference (also documented in docs/architecture.md §6):

    workspace:
      root: "~/.swe-agent"
    retries:
      max_debug_attempts: 3
      max_replan_attempts: 2
    models:
      <role>:                    # planner, researcher, developer, tester,
                                 # debugger, reviewer, security_reviewer,
                                 # github, verifier
        model: "claude-sonnet-4-5"
        api_key_env: "ANTHROPIC_API_KEY"   # env VAR NAME, never the key itself
        temperature: 0.2
        max_tokens: 8192

Unknown keys are rejected. Secrets are never stored in the config file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AGENT_ROLES = (
    "planner",
    "researcher",
    "developer",
    "tester",
    "debugger",
    "reviewer",
    "security_reviewer",
    "github",
    "verifier",
)


class WorkspaceConfig(BaseModel):
    """Where runs live. Default: ~/.swe-agent (runs under <root>/runs/<id>/)."""

    model_config = ConfigDict(extra="forbid")

    root: Path = Field(default=Path.home() / ".swe-agent")

    @field_validator("root")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()


class RetryConfig(BaseModel):
    """Bounded retry budgets for the state machine (see docs/agent-pipeline.md §1.4)."""

    model_config = ConfigDict(extra="forbid")

    max_debug_attempts: int = Field(default=3, ge=1, le=20)
    max_replan_attempts: int = Field(default=2, ge=0, le=10)


class ModelConfig(BaseModel):
    """LLM settings for one agent role. api_key_env is an env VAR NAME."""

    model_config = ConfigDict(extra="forbid")

    model: str = "claude-sonnet-4-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=8192, ge=1)


class ModelsConfig(BaseModel):
    """Per-role model configuration. Every AGENT_ROLES entry must be present."""

    model_config = ConfigDict(extra="forbid")

    planner: ModelConfig = Field(default_factory=ModelConfig)
    researcher: ModelConfig = Field(default_factory=lambda: ModelConfig(temperature=0.3))
    developer: ModelConfig = Field(
        default_factory=lambda: ModelConfig(temperature=0.1, max_tokens=16384)
    )
    tester: ModelConfig = Field(default_factory=lambda: ModelConfig(temperature=0.1))
    debugger: ModelConfig = Field(
        default_factory=lambda: ModelConfig(temperature=0.1, max_tokens=16384)
    )
    reviewer: ModelConfig = Field(default_factory=ModelConfig)
    security_reviewer: ModelConfig = Field(
        default_factory=lambda: ModelConfig(temperature=0.0)
    )
    github: ModelConfig = Field(
        default_factory=lambda: ModelConfig(max_tokens=4096)
    )
    verifier: ModelConfig = Field(
        default_factory=lambda: ModelConfig(temperature=0.0, max_tokens=4096)
    )

    def for_role(self, role: str) -> ModelConfig:
        if role not in AGENT_ROLES:
            raise KeyError(f"unknown agent role: {role!r}")
        return getattr(self, role)


class SweAgentConfig(BaseSettings):
    """Root configuration. YAML overrides defaults; env vars (SWE_*) override YAML."""

    model_config = SettingsConfigDict(
        env_prefix="SWE_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    retries: RetryConfig = Field(default_factory=RetryConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)


def load_config(path: Path | str | None = None) -> SweAgentConfig:
    """Load config from YAML at `path`, or from default locations.

    Search order when path is None: ./swe-agent.yaml, then
    ~/.swe-agent/config.yaml. Missing files fall back to defaults.
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path).expanduser())
    else:
        candidates.extend(
            [
                Path.cwd() / "swe-agent.yaml",
                Path.home() / ".swe-agent" / "config.yaml",
            ]
        )

    for candidate in candidates:
        if candidate.is_file():
            raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError(f"config root must be a mapping: {candidate}")
            return SweAgentConfig(**raw)

    if path is not None:
        raise FileNotFoundError(f"config file not found: {path}")
    return SweAgentConfig()
