"""Tests for the Autonomous Session Organ — faithful to discovery-engine's
app/services/autonomous_session.py turn-loop control."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from organ import decide

HERE = Path(__file__).parent


# --------------------------------------------------------------------------- #
# Session start: completed_turn = -1 -> run turn 0 with the directive prompt   #
# --------------------------------------------------------------------------- #
def test_session_start_runs_turn_zero():
    r = decide({"completed_turn": -1, "max_turns": 8,
                "directive": "Audit the API cost ledger."})
    out = r["output"]
    assert out["action"] == "run_turn"
    assert out["should_continue"] is True
    assert out["next_turn"] == 0
    assert out["stop_reason"] is None
    assert out["reply_classification"] is None  # no reply yet
    assert r["self_metric"]["confidence"] == 1.0


def test_turn_zero_prompt_interpolates_directive():
    r = decide({"completed_turn": -1, "directive": "Audit the API cost ledger."})
    assert "Audit the API cost ledger." in r["output"]["next_prompt"]
    assert "{directive}" not in r["output"]["next_prompt"]


def test_turn_zero_progress_pct_is_ten():
    # progress = 10 + int(0/8 * 80) == 10
    r = decide({"completed_turn": -1, "max_turns": 8, "directive": "x"})
    assert r["output"]["progress_pct"] == 10


# --------------------------------------------------------------------------- #
# Progress percentage formula: 10 + int(turn / max_turns * 80)                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("completed,max_turns,expected", [
    (-1, 8, 10),   # turn 0 -> 10
    (0, 8, 20),    # turn 1 -> 10 + int(1/8*80)=10+10=20
    (1, 8, 30),    # turn 2 -> 10 + int(2/8*80)=10+20=30
    (2, 8, 40),    # turn 3 -> 10 + 30 = 40
])
def test_progress_pct_formula(completed, max_turns, expected):
    r = decide({"completed_turn": completed, "max_turns": max_turns,
                "directive": "x", "last_reply": "still working, no summary"})
    assert r["output"]["action"] == "run_turn"
    assert r["output"]["progress_pct"] == expected


# --------------------------------------------------------------------------- #
# Scripted ladder vs continue-fallback                                         #
# --------------------------------------------------------------------------- #
def test_scripted_prompts_for_first_five_turns():
    # next_turn 0..4 are scripted (len(work_prompts) == 5)
    for completed in range(-1, 4):
        r = decide({"completed_turn": completed, "max_turns": 8,
                    "directive": "x", "last_reply": "working"})
        assert r["self_metric"]["decision_path"] == "run_turn_scripted"


def test_fallback_prompt_past_scripted_ladder():
    # completed turn 4 -> next_turn 5, past the 5 scripted prompts
    r = decide({"completed_turn": 4, "max_turns": 8,
                "directive": "x", "last_reply": "more analysis ongoing"})
    assert r["output"]["action"] == "run_turn"
    assert r["output"]["next_turn"] == 5
    assert r["self_metric"]["decision_path"] == "run_turn_fallback"
    assert "Continue your analysis" in r["output"]["next_prompt"]


# --------------------------------------------------------------------------- #
# Completion gate: turn >= 3 AND signals_completion(reply)                     #
# --------------------------------------------------------------------------- #
def test_completion_signal_after_turn_three_stops():
    r = decide({"completed_turn": 4, "max_turns": 8, "directive": "x",
                "last_reply": "Here is my executive summary of the work."})
    out = r["output"]
    assert out["action"] == "stop_completion"
    assert out["should_continue"] is False
    assert out["stop_reason"] == "persona_signalled_completion"
    assert out["reply_classification"]["signals_completion"] is True


def test_completion_signal_too_early_does_not_stop():
    # completed_turn 2 (< completion_min_turn 3): source `turn >= 3` is False,
    # so an early "in summary" must NOT early-complete.
    r = decide({"completed_turn": 2, "max_turns": 8, "directive": "x",
                "last_reply": "In summary, I'm just getting started."})
    assert r["output"]["action"] == "run_turn"
    # classification still reports the signal even though it didn't gate.
    assert r["output"]["reply_classification"]["signals_completion"] is True


def test_completion_signal_boundary_turn_three_stops():
    # completed_turn == 3 is exactly the boundary (>= 3) -> stops.
    r = decide({"completed_turn": 3, "max_turns": 8, "directive": "x",
                "last_reply": "To conclude, the findings are clear."})
    assert r["output"]["action"] == "stop_completion"


def test_no_completion_signal_keeps_running():
    r = decide({"completed_turn": 4, "max_turns": 8, "directive": "x",
                "last_reply": "Still investigating, lots more to do."})
    assert r["output"]["action"] == "run_turn"
    assert r["output"]["reply_classification"]["signals_completion"] is False


@pytest.mark.parametrize("phrase", [
    "executive summary", "in summary", "to conclude", "in conclusion",
    "that covers", "those are my key", "that's my full analysis",
])
def test_all_completion_phrases(phrase):
    r = decide({"completed_turn": 5, "max_turns": 8, "directive": "x",
                "last_reply": f"Right, {phrase} of it all."})
    assert r["output"]["action"] == "stop_completion"


# --------------------------------------------------------------------------- #
# Max-turns exhaustion                                                         #
# --------------------------------------------------------------------------- #
def test_max_turns_exhaustion_stops():
    # completed_turn 7 of max 8 -> next_turn 8 >= 8 -> stop.
    r = decide({"completed_turn": 7, "max_turns": 8, "directive": "x",
                "last_reply": "ongoing work, no conclusion"})
    out = r["output"]
    assert out["action"] == "stop_max_turns"
    assert out["stop_reason"] == "max_turns_reached"


def test_default_max_turns_is_eight():
    # No max_turns supplied -> defaults to 8. completed_turn 6 -> next 7 -> run.
    r = decide({"completed_turn": 6, "directive": "x", "last_reply": "go on"})
    assert r["output"]["action"] == "run_turn"
    # completed_turn 7 -> next 8 -> stop on the default cap.
    r2 = decide({"completed_turn": 7, "directive": "x", "last_reply": "go on"})
    assert r2["output"]["action"] == "stop_max_turns"


def test_completion_beats_max_turns_order():
    # On the last turn, a completion signal should still classify as completion
    # (source checks completion at the end of the turn body; here completion is
    # evaluated before the range gate for the just-finished turn).
    r = decide({"completed_turn": 7, "max_turns": 8, "directive": "x",
                "last_reply": "In conclusion, done."})
    assert r["output"]["action"] == "stop_completion"


# --------------------------------------------------------------------------- #
# Budget gate                                                                  #
# --------------------------------------------------------------------------- #
def test_budget_exceeded_stops_with_wrapup():
    r = decide({"completed_turn": 1, "max_turns": 8, "directive": "x",
                "last_reply": "progress", "budget": {"ok": False,
                                                      "spent": 2.5, "budget": 2.0}})
    out = r["output"]
    assert out["action"] == "stop_budget"
    assert out["stop_reason"] == "budget_exceeded"
    assert "BUDGET LIMIT REACHED" in out["next_prompt"]


def test_budget_ok_keeps_running():
    r = decide({"completed_turn": 1, "max_turns": 8, "directive": "x",
                "last_reply": "progress", "budget": {"ok": True,
                                                     "spent": 0.5, "budget": 2.0}})
    assert r["output"]["action"] == "run_turn"


def test_no_budget_key_means_no_cap():
    r = decide({"completed_turn": 1, "max_turns": 8, "directive": "x",
                "last_reply": "progress"})
    assert r["output"]["action"] == "run_turn"


def test_completion_beats_budget_order():
    # A completed turn that both signalled completion AND is over budget stops
    # as completion (the completion check is on the finished turn, evaluated
    # before the next turn's budget gate).
    r = decide({"completed_turn": 4, "max_turns": 8, "directive": "x",
                "last_reply": "Executive summary: all done.",
                "budget": {"ok": False, "spent": 9, "budget": 2}})
    assert r["output"]["action"] == "stop_completion"


# --------------------------------------------------------------------------- #
# Blocker classification (recorded, never a stop)                             #
# --------------------------------------------------------------------------- #
def test_blocker_classified_but_continues():
    r = decide({"completed_turn": 1, "max_turns": 8, "directive": "x",
                "last_reply": "I am blocked by missing data, need approval."})
    out = r["output"]
    assert out["action"] == "run_turn"  # blocker is recorded, not a stop
    assert out["reply_classification"]["is_blocker"] is True


@pytest.mark.parametrize("phrase", [
    "need input from", "blocker", "blocked by", "waiting on", "need approval",
    "can't proceed without", "require access to", "need clarification",
    "outside my access",
])
def test_all_blocker_phrases(phrase):
    r = decide({"completed_turn": 0, "max_turns": 8, "directive": "x",
                "last_reply": f"Note: {phrase} something."})
    assert r["output"]["reply_classification"]["is_blocker"] is True


# --------------------------------------------------------------------------- #
# Context overrides                                                            #
# --------------------------------------------------------------------------- #
def test_context_completion_min_turn_override():
    # Lower the gate to 1 -> a completion signal on turn 1 (completed_turn 1)
    # now stops.
    r = decide({"completed_turn": 1, "max_turns": 8, "directive": "x",
                "last_reply": "In summary, early but done."},
               {"completion_min_turn": 1})
    assert r["output"]["action"] == "stop_completion"


def test_context_custom_work_prompts():
    r = decide({"completed_turn": -1, "max_turns": 8, "directive": "x"},
               {"work_prompts": ["ONLY STEP"]})
    assert r["output"]["next_prompt"] == "ONLY STEP"


# --------------------------------------------------------------------------- #
# Fail-safe contract                                                           #
# --------------------------------------------------------------------------- #
def test_empty_state_stops_conservatively():
    r = decide({})
    assert r["output"]["action"] == "stop"
    assert r["output"]["should_continue"] is False
    assert r["self_metric"]["confidence"] < 0.5


def test_missing_completed_turn_stops():
    r = decide({"max_turns": 8, "directive": "x"})
    assert r["output"]["action"] == "stop"
    assert r["self_metric"]["decision_path"] == "no_completed_turn"


def test_non_dict_state_stops():
    r = decide("not a dict")  # type: ignore[arg-type]
    assert r["output"]["action"] == "stop"


def test_garbage_max_turns_defaults():
    r = decide({"completed_turn": -1, "max_turns": "abc", "directive": "x"})
    assert r["output"]["action"] == "run_turn"  # falls back to default 8


def test_determinism():
    s = {"completed_turn": 2, "max_turns": 8, "directive": "x",
         "last_reply": "still going"}
    assert decide(s) == decide(s)


def test_output_shape_complete():
    r = decide({"completed_turn": -1, "directive": "x"})
    assert set(r.keys()) == {"output", "rationale", "self_metric"}
    assert "confidence" in r["self_metric"]
    for k in ("action", "should_continue", "next_turn", "next_prompt",
              "progress_pct", "reply_classification", "stop_reason"):
        assert k in r["output"]


# --------------------------------------------------------------------------- #
# CLI / contract: stdin and ORGAN_INPUT both work, exit 0 on any verdict       #
# --------------------------------------------------------------------------- #
def test_cli_stdin():
    payload = json.dumps({"state": {"completed_turn": -1, "directive": "x"}})
    proc = subprocess.run([sys.executable, str(HERE / "organ.py")],
                          input=payload, capture_output=True, text=True)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["output"]["action"] == "run_turn"


def test_cli_organ_input_env(tmp_path):
    f = tmp_path / "in.json"
    f.write_text(json.dumps({"state": {"completed_turn": 7, "max_turns": 8,
                                       "last_reply": "no end yet"}}))
    proc = subprocess.run([sys.executable, str(HERE / "organ.py")],
                          capture_output=True, text=True,
                          env={"ORGAN_INPUT": str(f), "PATH": __import__("os").environ["PATH"]})
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["output"]["action"] == "stop_max_turns"


def test_cli_invalid_input_exits_nonzero():
    proc = subprocess.run([sys.executable, str(HERE / "organ.py")],
                          input="not json", capture_output=True, text=True)
    assert proc.returncode == 1


def test_all_samples_run_clean():
    sdir = HERE / "samples"
    files = sorted(sdir.glob("*.json"))
    assert len(files) >= 3
    for s in files:
        payload = json.loads(s.read_text())
        r = decide(payload["state"], payload.get("context"))
        assert "action" in r["output"]
        assert 0.0 <= r["self_metric"]["confidence"] <= 1.0
