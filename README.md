# swe-agent

CLI-first autonomous Software Engineering Agent.

> **Status:** M0/M1 — architecture docs and project foundation. No agent
> logic, LLM calls, or GitHub integration yet.

## Docs

- [Architecture](docs/architecture.md) — three-layer design, module map, config schema
- [Agent pipeline](docs/agent-pipeline.md) — state machine and the nine agents
- [Security](docs/security.md) — capabilities, prompt-injection defense, isolation
- [Execution model](docs/execution-model.md) — run lifecycle, shell rules, verification

## Development

```bash
uv sync
uv run swe --help
uv run swe doctor
uv run pytest
```
