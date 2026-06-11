# The organ contract

An **organ** is a **pure decider**. It reads facts, returns advice. It never acts —
the spine (GitHub) executes effects. This purity is load-bearing: it's what lets the
judgment layer run two organs on the *same* input and compare, with neither having
*done* anything irreversible.

## Interface (language-agnostic)

An organ is a program with a standard entrypoint.

- **Input** — one JSON object, read from **stdin**, or from the file named by the
  **`ORGAN_INPUT`** env var if set:
  ```json
  { "state": { ... }, "context": { ... } }
  ```
  - `state` (**required**) — the facts to judge. Per-organ schema.
  - `context` (optional) — run metadata / thresholds. The organ **must** work with
    `context` absent.

- **Output** — one JSON object to **stdout**:
  ```json
  { "output": { ... }, "rationale": "<str>", "self_metric": { "confidence": 0.0, ... } }
  ```
  - `output` — the decision (**advice only**, never an action).
  - `rationale` — human-readable *why*, derivable from `state` alone.
  - `self_metric` — `confidence` (0.0–1.0) is **required**; organ-specific counters
    (e.g. `checks_evaluated`) are encouraged. This is what the judgment layer scores.

- **Exit code** — `0` = ran and emitted a valid result. **A "no / not-ready" verdict
  is still exit 0** (the organ succeeded at deciding). Non-zero = the organ *itself*
  failed (unparseable input, internal error).

## Hard rules

1. **No side effects.** No network calls, no writes, no mutations. The organ is
   *handed* its facts in `state`; it never fetches them.
2. **Deterministic** given the same input.
3. **Fail-safe to the conservative verdict** on malformed/empty `state` — never a
   confident-wrong "yes". (A readiness organ with no facts returns *not ready*, low
   confidence.)
4. **Self-contained.** Stdlib-only where possible, so any runner can execute it.

See [`organs/_template/`](organs/_template/) for the minimal shape, and
[`organs/readiness_check/`](organs/readiness_check/) for a worked example.
