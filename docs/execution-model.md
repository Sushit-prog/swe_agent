# Execution Model

How a single run executes, end to end, and which components are allowed to
make which kinds of decisions.

Related: [architecture.md](architecture.md) · [agent-pipeline.md](agent-pipeline.md) ·
[security.md](security.md)

---

## 1. Run lifecycle

```
CLI parse → config load → Run row (CREATED) → workspace provision
   → orchestrator loop:
        agent_for(state) → LLM turns → tool requests → policy check
        → tool execution → deterministic checks → events
        → state machine transition (or raise on invalid)
   → terminal state (COMPLETED | FAILED | ESCALATED)
   → artifacts + trace finalized
```

1. **Entry.** `swe run` (future milestone) builds a `Run` from the issue
   reference and config. The run gets a unique `run-id` and a SQLite row.
2. **Provisioning.** `workspace/` creates `~/.swe-agent/runs/<run-id>/`,
   clones the target repo into `workspace/`, initializes `artifacts/`,
   `state.db`, `trace.jsonl`.
3. **Loop.** The orchestrator, not the model, owns the loop. Each iteration:
   - Look up the agent registered for the current state.
   - Build that agent's context: trusted run/plan/budget data + fenced
     untrusted blobs (security.md §3).
   - Call the provider (one provider-agnostic interface; anthropic is the
     first implementation). Model temperature/tokens come from the role's
     `ModelConfig`.
   - Parse the response into the agent's Pydantic output schema. Schema
     failure ⇒ retry once ⇒ `STEP_FAILED`-class event.
   - For each `tool_request`: policy engine decides (default DENY); approved
     requests execute via the tools layer inside the workspace; results
     (including denials) go back into the event stream.
   - Run whatever deterministic checks the state requires (e.g. Tester runs
     the test runner; Verifier runs the full suite).
   - Emit events; ask the transition table for the next state; update the
     `Run` row and budget counters.
4. **Termination.** Only the three terminal states end the loop. An invalid
   `(state, event)` pair raises a hard error and fails the run loudly rather
   than guessing.

### 1.1 Who decides what

| Decision | Decided by | Never by |
|---|---|---|
| Next state | Transition table + guards | LLM |
| Whether a tool runs | Policy engine (capability check) | LLM |
| Whether tests pass | Real test process exit code | LLM |
| Whether budgets remain | Counters on the `Run` row | LLM |
| PR target repo/branch | Runtime config | LLM |
| Push to remote | Runtime, post-approval | LLM |

---

## 2. Shell and process execution

All process execution goes through one runtime helper (tools layer):

- **Real binaries.** `git` is the installed `git`; test runners are the
  project's actual runners. No reimplementation.
- **Subprocess, captured.** `subprocess.run` with explicit argv lists (no
  `shell=True` string interpolation of model text), captured stdout/stderr,
  enforced timeout. Output size is truncated to a configured cap before it
  enters the LLM context or the trace.
- **Minimal environment.** Child processes receive a scrubbed env: PATH plus
  explicitly allowlisted variables (security.md §5). CWD is the run
  workspace.
- **Allowlists.** `shell.test` and read-only shell capabilities are matched
  against per-run allowlists (e.g. `pytest`, `python -m pytest`, `ruff check`,
  `mypy`). The Developer/Debugger `shell.any` capability is still filtered by
  a denylist of catastrophic commands (`rm -rf /`, `git push --force`,
  `shutdown`, etc.) — denylist as backstop, capability grants as the primary
  control.
- **Every execution is logged** to `trace.jsonl` with argv, exit code,
  duration, and the requesting agent role — before the output is shown to any
  model.

---

## 3. The verification loop (correctness core)

The LLM saying "done" means nothing. Deterministic verification is the only
path to `COMPLETED`:

1. **Step-level (TESTING).** Tester runs the runner for the current step.
   Pass ⇒ step is reviewable. Fail ⇒ `DEBUGGING` (bounded by
   `max_debug_attempts`) or `REPLANNING`.
2. **Global (VERIFICATION).** Verifier runs the full configured check suite
   (tests + lint + typecheck) from a clean evaluation of the workspace.
   `all_passed` (derived from real exit codes) ⇒ `CHECKS_PASS` ⇒ `PR_CREATION`.
3. **Budget exhaustion.** Debug budget exhausted on a failing suite ⇒
   `REPLANNING`. Replan budget exhausted ⇒ `ESCALATED` (human) — the run does
   not thrash. See agent-pipeline.md §1.4.
4. **Re-verification is mandatory** after any code-changing transition
   (`IMPLEMENTING`, `DEBUGGING` fixes). There is no edge into `PR_CREATION`
   that skips `VERIFICATION`.

---

## 4. Persistence (SQLite via SQLModel)

One database file per run at `runs/<run-id>/state.db` (a workspace-root
aggregate DB is a later option; per-run keeps isolation trivial).

Core tables (shapes, not final DDL):

| Table | Purpose | Key columns |
|---|---|---|
| `runs` | The run record | `id`, `issue_ref`, `state`, `debug_attempts_used`, `replan_attempts_used`, `created_at`, `updated_at` |
| `steps` | Plan steps from the Planner | `id`, `run_id`, `ordinal`, `description`, `success_criteria`, `status` |
| `events` | Append-only state-machine/agent events | `id`, `run_id`, `from_state`, `event`, `to_state`, `agent`, `payload_json`, `ts` |
| `tool_calls` | Full tool audit trail | `id`, `run_id`, `agent`, `tool`, `capability`, `decision` (GRANT/DENY), `argv_or_path`, `exit_code`, `ts` |

Budgets live on `runs` (authoritative counters). The `events` table is the
audit log: every transition is reconstructible. Model context never includes
raw budget arithmetic — it receives human-readable summaries the orchestrator
builds.

---

## 5. Tracing and logging

Two related but distinct streams:

- **`trace.jsonl` (per run, structured events).** One JSON object per line:
  state transitions, agent invocations (role, token usage, latency), tool
  calls with GRANT/DENY decision and result, verification check outcomes.
  This is the debugging and post-mortem artifact.
- **Process logging (`logging`).** Operator-facing, verbosity-controlled
  (`-v`/`--verbose`, or config), secret-redacting (security.md §5). Format is
  structured (key=value / JSON) so it can be grepped or shipped.

Rules:

- Never log secrets, full env dumps, or raw API keys — redaction filter is
  always installed, including at DEBUG level.
- Agent "reasoning" is not chain-of-thought logged; we log the structured
  output models and events, which are the auditable artifacts anyway.
- Latency/token metrics per agent role are emitted on every LLM call — early
  visibility into cost as later milestones add real calls.

---

## 6. Failure handling summary

| Failure | Handling |
|---|---|
| LLM output fails schema | Retry once with validation errors; then `STEP_FAILED`-class event |
| Tool request denied by policy | Structured denial in trace + to agent; agent may adapt; repeated denials can route to `RECOVERING` |
| Shell command timeout/crash | Captured as failed check / failed step; never kills the orchestrator |
| Invalid transition requested | Hard error — orchestrator fails the run loudly (bug indicator) |
| Budget exhausted | `REPLANNING` → `ESCALATED` per transition table |
| Provider/API error | Retry with backoff (provider layer); persistent failure ⇒ `FAILED` |

---

## 7. Concurrency and idempotency (current stance)

M0/M1 is single-run, synchronous. The design leaves room for concurrent runs
(disjoint run directories, per-run DB) but the orchestrator loop itself is
sequential per run: one agent active at a time, one state, deterministic
ordering. Parallel tool execution is explicitly out of scope until there is a
measured need — it complicates the audit trail for little gain at this stage.
