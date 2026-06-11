# organ-autonomous-session-v2

> **Facet note.** This repo extracts the **turn-loop control** facet of
> `autonomous_session.py` (the heart of `run_autonomous_session`). The sibling
> repo [`organ-autonomous-session`](https://github.com/Data-Flow-Advisory/organ-autonomous-session)
> already extracts the *complementary* **session-type router** facet (which
> session path to dispatch: pa_monitor / sdr / spec / team_spec / generic). The
> two are not redundant — they cover different pure decisions in the same source
> file. This is `-v2` to avoid clobbering that green sibling, not to supersede
> it.

A **pure decider** for the turn-loop control of a persona's autonomous work
session, extracted from discovery-engine
[`app/services/autonomous_session.py`](https://github.com/Data-Flow-Advisory/discovery-engine)
(`run_autonomous_session`'s `for turn in range(max_turns)` body plus the helpers
`_mentions_blocker`, `_signals_completion`, and the prompt builder
`_build_work_prompts`).

> An organ reads facts and returns **advice**. It never acts — the spine
> executes effects. See [`CONTRACT.md`](CONTRACT.md).

## What it decides

Given the state at a single turn boundary — the turn just completed, its reply
text, the running budget, and the directive — it returns the next control
action and the exact prompt the spine should send next:

| `action` | when | spine does |
|----------|------|------------|
| `run_turn` | budget healthy, loop not exhausted, no completion signal | send `next_prompt`, run the model, report `progress_pct` |
| `stop_completion` | completed turn ≥ 3 **and** its reply matched a completion phrase | finalise the session |
| `stop_max_turns` | `range(max_turns)` exhausted | finalise the session |
| `stop_budget` | `budget.ok == false` going into the next turn | send the wrap-up `next_prompt`, then finalise |
| `stop` | empty / malformed state (fail-safe) | do **not** burn another turn |

Every reply is also classified — `reply_classification.is_blocker` /
`signals_completion` — so the spine can record blockers (which never stop the
loop) and gate early completion.

Decision order mirrors the source exactly: the post-turn completion check fires
first, then the `range(max_turns)` gate, then the top-of-turn budget gate,
otherwise run the next turn.

### What stays in the spine (impure)

Calling the model, executing tools, reading `job_queue.check_budget`,
persisting Question/Answer rows, committing the Interview, fact extraction, and
notifications. The organ is *handed* its facts and only advises the control
flow — no LLM/DB/network/clock calls.

## Usage

```bash
# stdin
echo '{"state": {"completed_turn": -1, "directive": "Audit the cost ledger."}}' | python3 organ.py

# or via a file
ORGAN_INPUT=samples/completion_signalled.json python3 organ.py
```

Input is `{ "state": {...}, "context": {...} }`; output is
`{ "output": {...}, "rationale": "...", "self_metric": {"confidence": ...} }`.
Full schema in the [`organ.py`](organ.py) docstring.

## Faithful constants

- `MAX_TURNS = 8` (default `max_turns`)
- early-completion gate `turn >= 3` (`completion_min_turn`)
- progress formula `10 + int(turn / max_turns * 80)`
- blocker / completion signal phrase lists — verbatim from the source
- the scripted 5-step prompt ladder + generic continue-fallback + budget wrap-up
  message — verbatim from the source

## Tests

```bash
pip install pytest && python -m pytest -v
```

The `conformance` GitHub Action shadow-runs the organ on every file in
`samples/` and reports the verdicts to the job summary, then runs the test
suite.
