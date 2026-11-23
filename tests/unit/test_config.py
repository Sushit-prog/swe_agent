from pathlib import Path

import pytest
from pydantic import ValidationError

from swe_agent.config.schema import AGENT_ROLES, SweAgentConfig, load_config


def test_valid_config_with_overrides() -> None:
    cfg = SweAgentConfig(
        workspace={
            "root": "~/custom-swe",
            "clone_timeout_seconds": 60,
            "max_repo_size_mb": 100,
        },
        retries={"max_debug_attempts": 5, "max_replan_attempts": 1},
        models={"planner": {"model": "custom-model", "max_tokens": 4096}},
        github={"token_env": "MY_GITHUB_TOKEN"},
    )

    assert cfg.workspace.root == Path.home() / "custom-swe"
    assert cfg.workspace.clone_timeout_seconds == 60
    assert cfg.retries.max_debug_attempts == 5
    assert cfg.models.for_role("planner").model == "custom-model"
    assert cfg.models.for_role("planner").max_tokens == 4096
    assert cfg.models.for_role("researcher").temperature == 0.3
    assert cfg.github.token_env == "MY_GITHUB_TOKEN"
    for role in AGENT_ROLES:
        assert cfg.models.for_role(role) is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"workspace": {"root": "/tmp/x", "typo": 1}},
        {"retries": {"max_debug_attempts": 3, "extra": True}},
        {"models": {"planner": {"model": "m", "temperatur": 0.1}}},
        {"models": {"unknown_role": {"model": "m"}}},
        {"github": {"token_env": "GITHUB_TOKEN", "token": "secret"}},
        {"unknown_section": {}},
    ],
)
def test_extra_keys_rejected(payload: dict) -> None:
    with pytest.raises(ValidationError):
        SweAgentConfig(**payload)


def test_extra_key_in_yaml_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "swe-agent.yaml"
    config_file.write_text(
        'workspace:\n  root: "/tmp/x"\n  typo: 1\n', encoding="utf-8"
    )

    with pytest.raises(ValidationError):
        load_config(config_file)


def test_swe_env_vars_override_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SWE_WORKSPACE__ROOT", str(tmp_path / "env-root"))
    monkeypatch.setenv("SWE_RETRIES__MAX_DEBUG_ATTEMPTS", "7")
    monkeypatch.setenv("SWE_MODELS__PLANNER__MODEL", "env-model")
    monkeypatch.setenv("SWE_GITHUB__TOKEN_ENV", "ENV_GITHUB_TOKEN")

    cfg = load_config()

    assert cfg.workspace.root == tmp_path / "env-root"
    assert cfg.retries.max_debug_attempts == 7
    assert cfg.models.planner.model == "env-model"
    assert cfg.github.token_env == "ENV_GITHUB_TOKEN"


def test_only_env_var_names_are_stored(monkeypatch, tmp_path: Path) -> None:
    secret = "sk-ant-DO-NOT-STORE-IN-CONFIG"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    config_file = tmp_path / "swe-agent.yaml"
    config_file.write_text(
        'models:\n  planner:\n    api_key_env: "ANTHROPIC_API_KEY"\n'
        'github:\n  token_env: "GITHUB_TOKEN"\n',
        encoding="utf-8",
    )

    cfg = load_config(config_file)

    assert cfg.models.planner.api_key_env == "ANTHROPIC_API_KEY"
    assert cfg.github.token_env == "GITHUB_TOKEN"
    assert secret not in str(cfg.model_dump())
    for role in AGENT_ROLES:
        assert cfg.models.for_role(role).api_key_env == "ANTHROPIC_API_KEY"


def test_for_role_rejects_unknown_role() -> None:
    with pytest.raises(KeyError):
        SweAgentConfig().models.for_role("not-a-role")
