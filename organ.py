#!/usr/bin/env python3
"""
Autonomous Session Organ — extracted decision logic from discovery-engine.

A pure decider for the **turn-loop control** of a persona's autonomous work
session. Given the state at a single turn boundary — the turn just completed,
its reply text, the running budget, and the directive — it returns the next
control action (run another turn / stop) plus the exact prompt the spine should
send next and the progress percentage to report. It also classifies the latest
reply (blocker / completion-signal) so the spine can record blockers and gate
early completion.

This is the pure core of discovery-engine's
``app/services/autonomous_session.py`` work loop
(``run_autonomous_session``'s ``for turn in range(max_turns)`` body plus its two
helpers ``_mentions_blocker`` and ``_signals_completion`` and the prompt builder
``_build_work_prompts``). The impure parts are the spine's job:

  * calling the model (``inference_router.run_chat``), executing tools, and
    appending to the conversation — the organ never talks to an LLM;
  * reading the budget ledger (``job_queue.check_budget``) — the result is
    *handed* to the organ in ``state.budget``;
  * persisting Question/Answer rows, committing the Interview, firing fact
    extraction + notifications — the organ only *advises* the control flow.

The loop's control structure is reproduced faithfully:

  for turn in range(max_turns):
      if budget not ok:  append wrap-up, break        # -> stop_budget
      prompt = work_prompts[turn] or continue-fallback # -> next_prompt
      ... run the turn ...
      if mentions_blocker(reply): record blocker        # -> is_blocker
      if turn >= 3 and signals_completion(reply): break # -> stop_completion

The organ collapses one turn boundary into a single decision. Decision order
mirrors the source: the completion check (post-turn, on the just-finished
``completed_turn``'s reply) fires first; then the ``range(max_turns)`` exhaustion
gate; then the top-of-turn budget gate; otherwise run the next turn.

Contract (see CONTRACT.md):
  INPUT state: {
    "completed_turn": 4 | -1,    # 0-indexed turn JUST completed; -1 == session
                                 # start (nothing run yet). REQUIRED.
    "max_turns": 8,              # loop bound (defaults to MAX_TURNS=8)
    "last_reply": "..." | null,  # text the persona produced on completed_turn
                                 # (null when completed_turn == -1)
    "budget": {                  # snapshot of job_queue.check_budget going into
                                 # the NEXT turn (optional — absent == no cap)
      "ok": true,                # false == spend has reached/exceeded the cap
      "spent": 0.50,             # informational pass-through
      "budget": 2.00             # informational pass-through
    },
    "directive": "..."           # the persona's directive (for turn-0 prompt)
  }

  INPUT context (all optional — organ works with context absent): {
    "completion_min_turn": 3,    # earliest completed_turn that may early-stop
    "fallback_prompt": "...",    # prompt used past the scripted work_prompts
    "budget_wrapup_prompt": "...",# message sent when the budget gate trips
    "work_prompts": ["...", ...] # override the scripted 5-step prompt ladder
  }

  OUTPUT: {
    "output": {
      "action": "run_turn"|"stop_budget"|"stop_completion"|"stop_max_turns"|"stop",
      "should_continue": bool,        # action == "run_turn"
      "next_turn": int | null,        # 0-indexed turn to run next (run_turn only)
      "next_prompt": str | null,      # exact prompt to send (run_turn/stop_budget)
      "progress_pct": int | null,     # 10 + int(turn/max_turns*80), run_turn only
      "reply_classification": {       # of last_reply (null when no reply)
        "is_blocker": bool,
        "signals_completion": bool
      } | null,
      "stop_reason": str | null       # set on any stop_* action
    },
    "rationale": str,
    "self_metric": { "confidence": float, "decision_path": str, "action": str }
  }

The organ is pure: all inputs via JSON, no LLM/DB/network/clock calls,
deterministic, and fail-safe to the conservative verdict — on malformed or empty
``state`` it returns ``action="stop"`` (do NOT burn another turn on garbage
state) with low confidence, never a confident-wrong "run_turn". Stdlib-only.
"""
from __future__ import annotations

import json
import os
import sys

# Constants mirror discovery-engine's autonomous_session.py exactly:
#   MAX_TURNS = 8; the early-completion gate `turn >= 3`; the progress formula
#   `10 + int((turn / max_turns) * 80)`.
_DEFAULT_MAX_TURNS = 8
_DEFAULT_COMPLETION_MIN_TURN = 3

# Blocker signals — verbatim from _mentions_blocker().
_BLOCKER_SIGNALS = (
    "need input from",
    "blocker",
    "blocked by",
    "waiting on",
    "need approval",
    "can't proceed without",
    "require access to",
    "need clarification",
    "outside my access",
)

# Completion signals — verbatim from _signals_completion().
_COMPLETION_SIGNALS = (
    "executive summary",
    "in summary",
    "to conclude",
    "in conclusion",
    "that covers",
    "those are my key",
    "that's my full analysis",
)

# The scripted 5-step prompt ladder — verbatim from _build_work_prompts().
# `{directive}` in step 0 is filled from state.directive.
_WORK_PROMPTS = [
    (
        "Your directive is: {directive}\n\n"
        "Start by breaking this down FROM YOUR SPECIFIC EXPERTISE. "
        "What are the key questions that only someone in YOUR role "
        "can answer? What do you already know from your accumulated "
        "knowledge? Do not cover ground that belongs to other roles."
    ),
    (
        "Good. Now go deeper on the most important aspect "
        "WITHIN YOUR DOMAIN. What's the core finding or recommendation "
        "that only YOUR role can make? "
        "Support it with evidence from your knowledge base."
    ),
    (
        "What are the risks or concerns SPECIFIC TO YOUR DOMAIN? "
        "What could go wrong in YOUR area? "
        "What assumptions are you making that might not hold? "
        "Note any information gaps as blockers."
    ),
    (
        "Now synthesise everything into concrete recommendations "
        "that only YOUR role can own. "
        "Be specific: what exactly should be done, by whom, and by when? "
        "What are the dependencies?\n\n"
        "If your work requires communicating with a human (a prospect, "
        "stakeholder, or colleague), call the draft_email tool to create "
        "a draft. A human reviews and approves it at /admin/drafts before "
        "anything sends — you can NEVER send email yourself."
    ),
    (
        "Final step: write a brief executive summary of your work on "
        "this directive FROM YOUR ROLE'S PERSPECTIVE. "
        "Lead with the headline finding, then key "
        "recommendations, then blockers or risks. Keep it under 200 words."
    ),
]

# The generic continue-prompt the source falls back to past the scripted ladder.
_FALLBACK_PROMPT = (
    "Continue your analysis. What else have you found? "
    "If you've covered everything, summarise your key findings "
    "and action items."
)

# The wrap-up message the source appends when the budget gate trips.
_BUDGET_WRAPUP_PROMPT = (
    "BUDGET LIMIT REACHED. Wrap up immediately: summarise "
    "what you've accomplished and list any remaining items "
    "as action items for the next session."
)


def _to_int(value, default: int) -> int:
    """Coerce a value to int, falling back to ``default`` on None/garbage."""
    try:
        if value is None:
            return int(default)
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _matches(text, signals) -> bool:
    """True if any signal substring appears in ``text`` (case-insensitive).
    Mirrors the `any(s in lower for s in signals)` pattern in the source."""
    if not isinstance(text, str):
        return False
    lower = text.lower()
    return any(s in lower for s in signals)


def _classify_reply(last_reply) -> dict | None:
    """Classify a reply as blocker / completion-signal, or None when absent."""
    if last_reply is None:
        return None
    return {
        "is_blocker": _matches(last_reply, _BLOCKER_SIGNALS),
        "signals_completion": _matches(last_reply, _COMPLETION_SIGNALS),
    }


def _prompt_for_turn(turn: int, directive: str, work_prompts: list) -> str:
    """The prompt the source sends for ``turn``: the scripted step if one exists
    (with {directive} filled on step 0), else the generic continue-fallback."""
    if 0 <= turn < len(work_prompts):
        tmpl = work_prompts[turn]
        if isinstance(tmpl, str) and "{directive}" in tmpl:
            return tmpl.replace("{directive}", directive)
        return tmpl
    return _FALLBACK_PROMPT


def decide(state: dict, context: dict | None = None) -> dict:
    """Decide the next turn-loop control action for an autonomous session.

    Pure, deterministic, fail-safe to ``action="stop"`` (never a confident-wrong
    "run another turn") on malformed or empty ``state``."""
    context = context or {}

    try:
        if not isinstance(state, dict) or not state:
            return _stop_unknown(
                "No state supplied — cannot judge the loop; stopping (do not "
                "burn another turn on empty state).", "empty_state")

        max_turns = _to_int(state.get("max_turns"), _DEFAULT_MAX_TURNS)
        if max_turns <= 0:
            max_turns = _DEFAULT_MAX_TURNS
        completion_min_turn = _to_int(
            context.get("completion_min_turn"), _DEFAULT_COMPLETION_MIN_TURN)

        work_prompts = context.get("work_prompts")
        if not isinstance(work_prompts, list) or not work_prompts:
            work_prompts = _WORK_PROMPTS
        fallback_prompt = context.get("fallback_prompt") or _FALLBACK_PROMPT
        budget_wrapup = context.get("budget_wrapup_prompt") or _BUDGET_WRAPUP_PROMPT

        directive = state.get("directive")
        directive = directive if isinstance(directive, str) else ""

        # `completed_turn` is the 0-indexed turn just finished; -1 == session
        # start (the for-loop hasn't entered yet).
        if "completed_turn" not in state:
            return _stop_unknown(
                "state.completed_turn missing — cannot place the loop; stopping "
                "(conservative).", "no_completed_turn")
        completed_turn = _to_int(state.get("completed_turn"), -1)

        last_reply = state.get("last_reply")
        last_reply = last_reply if isinstance(last_reply, str) else None
        classification = _classify_reply(last_reply)

        # --- (a) post-turn completion gate (source: end-of-loop break) --------
        # `if turn >= 3 and _signals_completion(reply): break` — only fires for a
        # turn that actually ran (completed_turn >= 0) and produced a reply.
        if (
            completed_turn >= completion_min_turn
            and classification is not None
            and classification["signals_completion"]
        ):
            return {
                "output": {
                    "action": "stop_completion",
                    "should_continue": False,
                    "next_turn": None,
                    "next_prompt": None,
                    "progress_pct": None,
                    "reply_classification": classification,
                    "stop_reason": "persona_signalled_completion",
                },
                "rationale": (
                    f"Persona signalled completion on turn {completed_turn + 1} "
                    f"(>= min turn {completion_min_turn + 1}) — its reply matched a "
                    "completion phrase. Stop the loop here, exactly as the source "
                    "breaks on `turn >= 3 and _signals_completion(reply)`."
                ),
                "self_metric": {
                    "confidence": 1.0,
                    "decision_path": "completion_signal",
                    "action": "stop_completion",
                },
            }

        next_turn = completed_turn + 1

        # --- (b) range(max_turns) exhaustion ---------------------------------
        if next_turn >= max_turns:
            return {
                "output": {
                    "action": "stop_max_turns",
                    "should_continue": False,
                    "next_turn": None,
                    "next_prompt": None,
                    "progress_pct": None,
                    "reply_classification": classification,
                    "stop_reason": "max_turns_reached",
                },
                "rationale": (
                    f"Turn {completed_turn + 1} of {max_turns} completed — the "
                    f"`for turn in range({max_turns})` loop is exhausted. Stop and "
                    "finalise the session."
                ),
                "self_metric": {
                    "confidence": 1.0,
                    "decision_path": "max_turns",
                    "action": "stop_max_turns",
                },
            }

        # --- (c) top-of-turn budget gate (source: check_budget -> break) -----
        budget = state.get("budget")
        if isinstance(budget, dict) and budget.get("ok") is False:
            spent = budget.get("spent")
            cap = budget.get("budget")
            return {
                "output": {
                    "action": "stop_budget",
                    "should_continue": False,
                    "next_turn": None,
                    # The source still emits a final wrap-up user message before
                    # breaking — surface it so the spine can send it.
                    "next_prompt": budget_wrapup,
                    "progress_pct": None,
                    "reply_classification": classification,
                    "stop_reason": "budget_exceeded",
                },
                "rationale": (
                    "Budget exceeded going into turn "
                    f"{next_turn + 1} (spent={spent}, budget={cap}). Send the "
                    "wrap-up message and stop, mirroring the source's "
                    "`if not budget_check['ok']: break`."
                ),
                "self_metric": {
                    "confidence": 1.0,
                    "decision_path": "budget_exceeded",
                    "action": "stop_budget",
                },
            }

        # --- (d) run the next turn -------------------------------------------
        # Prompt selection: scripted ladder, else the generic continue-fallback.
        if 0 <= next_turn < len(work_prompts):
            next_prompt = _prompt_for_turn(next_turn, directive, work_prompts)
        else:
            next_prompt = fallback_prompt
        progress_pct = 10 + int((next_turn / max_turns) * 80)

        return {
            "output": {
                "action": "run_turn",
                "should_continue": True,
                "next_turn": next_turn,
                "next_prompt": next_prompt,
                "progress_pct": progress_pct,
                "reply_classification": classification,
                "stop_reason": None,
            },
            "rationale": (
                f"Run turn {next_turn + 1} of {max_turns}: budget healthy, loop "
                "not exhausted, no completion signal yet. Sending the "
                + ("scripted step prompt" if next_turn < len(work_prompts)
                   else "generic continue-fallback prompt")
                + f"; progress {progress_pct}%."
                + ("" if classification is None
                   else f" (Prior reply blocker={classification['is_blocker']}.)")
            ),
            "self_metric": {
                "confidence": 1.0,
                "decision_path": (
                    "run_turn_scripted" if next_turn < len(work_prompts)
                    else "run_turn_fallback"),
                "action": "run_turn",
            },
        }
    except Exception as e:  # noqa: BLE001 — fail-safe to the conservative verdict
        return _stop_unknown(
            f"Organ error ({e}) — failing safe to stop (no further turns).",
            "decision_error", confidence=0.0)


def _stop_unknown(rationale: str, decision_path: str, *,
                  confidence: float = 0.3) -> dict:
    """Conservative no-verdict result: stop, NEVER run another turn."""
    return {
        "output": {
            "action": "stop",
            "should_continue": False,
            "next_turn": None,
            "next_prompt": None,
            "progress_pct": None,
            "reply_classification": None,
            "stop_reason": "unknown",
        },
        "rationale": rationale,
        "self_metric": {
            "confidence": confidence,
            "decision_path": decision_path,
            "action": "stop",
        },
    }


def main() -> int:
    path = os.environ.get("ORGAN_INPUT")
    raw = open(path).read() if path else sys.stdin.read()
    try:
        payload = json.loads(raw)
        state = payload["state"]
    except Exception as e:
        print(json.dumps({"error": f"invalid input: {e}"}), file=sys.stderr)
        return 1
    print(json.dumps(decide(state, payload.get("context")), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
