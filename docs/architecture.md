# swe-agent Architecture

`swe-agent` is a CLI-first autonomous Software Engineering Agent. Given an issue
(GitHub issue text or a local task description), it plans, researches,
implements, tests, reviews, and opens a pull request — with hard safety bounds
enforced outside the LLM.

> **Status:** M0/M1 — architecture and project foundation only. No agent logic,
> LLM calls, or GitHub integration is implemented yet.

---

## 1. Core design principle: three-layer separation

Every design decision in this codebase falls into exactly one of three layers.
The separation is absolute: **a layer never delegates its authority to another
layer.**

| Layer | Responsibility | Trust level | May call LLM? |
|---|---|---|---|
| **LLM reasoning** | Propose plans, patches, analyses, review findings | Untrusted advisor | Yes (it *is* the LLM) |
| **Runtime authority** | Enforce capability grants, sandbox the workspace, apply budgets, schedule transitions | Trusted, deterministic code | No |
| **Deterministic verification** | Run tests/lint/typechecks, evaluate outputs against schemas, decide pass/fail | Trusted, deterministic code | No |

### Why this matters

An autonomous coding agent that "trusts its model" is a prompt-injection
machine: any repository file, issue comment, or test output can contain text
that steers the model. We never let the model decide what it is allowed to do.

Concretely:

- The LLM **proposes** a shell command; the runtime authority **decides**
  whether that command is permitted for the current agent's capability set.
- The LLM **claims** a task is done; deterministic verification **decides** by
  running the actual test suite.
- The LLM **suggests** the next state; the hand-written transition table
  **decides** whether that transition is legal and whether retry budgets remain.

Rule of thumb used throughout the codebase: *if a decision affects safety,
correctness, or termination, it lives in runtime authority or deterministic
verification — never in a prompt.*

---

## 2. System overview

```
                ┌──────────────────────────────────────────────┐
                │                   CLI (Typer)                │
                │   swe run · swe doctor · swe config · …      │
                └───────────────────────┬──────────────────────┘
                                        │
                ┌───────────────────────▼──────────────────────┐
                │              Orchestrator                    │
                │  drives one Run through the state machine    │
                └───────┬───────────────┬──────────────┬───────┘
                        │               │              │
              ┌─────────▼───┐   ┌───────▼──────┐  ┌────▼─────────┐
              │ State machine│   │  9 Agents    │  │  Policy /    │
              │ (enum +      │   │ (each a thin │  │  Capability  │
              │  table, no   │   │  LLM wrapper │  │  engine      │
              │  library)    │   │  with Pydantic│ │  (default    │
              └─────────┬───┘   │  output)     │  │   DENY)      │
                        │       └───────┬──────┘  └────┬─────────┘
                        │               │              │
                ┌───────▼───────────────▼──────────────▼───────┐
                │              Runtime (tools layer)           │
                │  shell/git (subprocess) · fs · github · …    │
                └───────┬───────────────┬──────────────┬───────┘
                        │               │              │
              ┌─────────▼───┐   ┌───────▼──────┐  ┌────▼─────────┐
              │ Workspace   │   │ Persistence  │  │ Tracing /    │
              │ (~/.swe-    │   │ (SQLite via  │  │ Logging      │
              │  agent/runs)│   │  SQLModel)   │  │              │
              └─────────────┘   └──────────────┘  └──────────────┘
                        ▲
                        │ all LLM traffic goes through
              ┌─────────┴───────────────────────────┐
              │ Providers (provider-agnostic iface) │
              │  first impl: anthropic SDK          │
              └─────────────────────────────────────┘
```

**Data flow for one run:** CLI parses args → loads config (YAML +
pydantic-settings) → orchestrator creates a `Run` (state `CREATED`) and a
workspace under `~/.swe-agent/runs/<run-id>/` → the state machine advances by
dispatching the agent registered for the current state → each agent makes LLM
calls through the provider interface → every tool the agent attempts goes
through the policy engine → deterministic verification gates every transition
into "success-ish" states → run ends in `COMPLETED`, `FAILED`, or `ESCALATED`,
with full state and event history persisted.

---

## 3. Module map

```
src/swe_agent/
├── cli/           Typer application: argument parsing, Rich output. No logic.
├── core/          Cross-cutting types, errors, IDs.
├── agents/        The 9 agents. Thin: prompt assembly + Pydantic output parsing.
│                  No tool execution, no transition decisions.
├── orchestrator/  The run loop: picks the agent for the current state, feeds
│                  it context, hands results to verification, asks the state
│                  machine for the next state.
├── tools/         Concrete tools (shell, git, filesystem, github). Each tool
│                  declares the capability it requires; none executes without
│                  a policy check.
├── policy/        Capability engine. Default DENY. Per-agent grant tables.
│                  The runtime-authority layer's enforcement point.
├── workspace/     Creation/teardown of ~/.swe-agent/runs/<run-id>/, path
│                  resolution, isolation invariants.
├── providers/     Provider-agnostic LLM interface; anthropic implementation.
├── github/        PyGithub wrapper (issue fetch, PR creation). Later milestone.
├── state/         State enum + explicit transition table + retry budgets.
├── persistence/   SQLModel models + SQLite session management.
├── tracing/       Structured logging setup, event stream for a run.
└── config/        pydantic-settings models + YAML loader.
```

---

## 4. Technology decisions (locked)

| Concern | Decision | Notes |
|---|---|---|
| Language / runtime | Python ≥ 3.11 | `requires-python = ">=3.11"` |
| Packaging | `uv` | `uv sync`, `uv run` |
| CLI | Typer, output via Rich | `swe` console script |
| LLM | `anthropic` SDK behind provider-agnostic interface | Interface in `providers/`; never import SDK outside that package |
| Git | Shell out to real `git` via `subprocess` | Never GitPython — we want exact upstream semantics and a single auditable subprocess boundary |
| GitHub API | PyGithub | Wrapped in `github/` |
| Structured I/O | Pydantic v2 | Every agent output is a validated model |
| State machine | Hand-written enum + transition table | No library; the table *is* the spec |
| Persistence | SQLite via SQLModel | One DB file per workspace root |
| Config | YAML + pydantic-settings | See §6 |
| Logging | stdlib `logging`, structured, secret-redacting | See `tracing/` |

Dependency policy: the locked set is `typer, rich, anthropic, pydantic,
pydantic-settings, sqlmodel, PyGithub, pytest, pyyaml`. Anything else requires
an explicit decision.

---

## 5. State machine (summary)

Full specification — including the transition diagram, retry budgets, and the
nine agents — lives in [agent-pipeline.md](agent-pipeline.md). The 16 states:

```
CREATED · PLANNING · RESEARCHING · IMPLEMENTING · TESTING · DEBUGGING ·
REVIEWING · SECURITY_REVIEW · VERIFICATION · PR_CREATION · AWAITING_APPROVAL ·
COMPLETED · FAILED · RECOVERING · REPLANNING · ESCALATED
```

Two invariants enforced by the transition table:

1. **No state reaches `COMPLETED` without passing `VERIFICATION`** (which runs
   the deterministic test suite).
2. **Retry loops are bounded**: `max_debug_attempts` and `max_replan_attempts`
   come from config; exhausting them routes to `RECOVERING` and then
   `ESCALATED` or `FAILED` — never an infinite loop.

---

## 6. Configuration schema

Config is a YAML file loaded through pydantic-settings. Default search order:
`./swe-agent.yaml` → `~/.swe-agent/config.yaml`. API keys are **never** in the
file — only the *name of the environment variable* that holds them.

```yaml
# swe-agent.yaml — full schema with defaults
workspace:
  root: "~/.swe-agent"          # runs live under <root>/runs/<run-id>/

retries:
  max_debug_attempts: 3         # TESTING→DEBUGGING→TESTING cycles per issue
  max_replan_attempts: 2        # REPLANNING entries per run

models:
  # One ModelConfig per agent role. api_key_env is the env VAR NAME, not the key.
  planner:
    model: "claude-sonnet-4-5"
    api_key_env: "ANTHROPIC_API_KEY"
    temperature: 0.2
    max_tokens: 8192
  researcher:   { model: "...", api_key_env: "ANTHROPIC_API_KEY", temperature: 0.3, max_tokens: 8192 }
  developer:    { model: "...", api_key_env: "ANTHROPIC_API_KEY", temperature: 0.1, max_tokens: 16384 }
  tester:       { model: "...", api_key_env: "ANTHROPIC_API_KEY", temperature: 0.1, max_tokens: 8192 }
  debugger:     { model: "...", api_key_env: "ANTHROPIC_API_KEY", temperature: 0.1, max_tokens: 16384 }
  reviewer:     { model: "...", api_key_env: "ANTHROPIC_API_KEY", temperature: 0.2, max_tokens: 8192 }
  security_reviewer: { model: "...", api_key_env: "ANTHROPIC_API_KEY", temperature: 0.0, max_tokens: 8192 }
  github:       { model: "...", api_key_env: "ANTHROPIC_API_KEY", temperature: 0.2, max_tokens: 4096 }
  verifier:     { model: "...", api_key_env: "ANTHROPIC_API_KEY", temperature: 0.0, max_tokens: 4096 }
```

Pydantic models live in `src/swe_agent/config/`. Unknown keys are rejected;
every field above is typed. `swe doctor` validates that each `api_key_env`
named in the active config is actually set in the environment (env var *names*
are safe to log; values never are).

---

## 7. Documentation map

| Doc | Contents |
|---|---|
| [architecture.md](architecture.md) (this file) | Layers, system overview, module map, config schema |
| [agent-pipeline.md](agent-pipeline.md) | State machine, agents, retry budgets |
| [security.md](security.md) | Capabilities, prompt-injection defense, secrets, workspace isolation |
| [execution-model.md](execution-model.md) | Run lifecycle, shell rules, verification loop, persistence, tracing |
