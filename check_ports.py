#!/usr/bin/env python3
"""Connection-standard ports check for the autonomous-session organ.

Asserts, with no third-party deps:

  1. ports.json parses and has the {"inputs": [...], "outputs": [...]} shape,
     each entry a dict with a "name" and a "type" (inputs also carry
     "required": bool).
  2. types.json parses and every type declared in ports.json exists in the
     vendored type vocabulary (types.json["types"]).
  3. decide() READS every declared input name from `state` — verified by a
     static scan of organ.py source for `state.get("<name>")`,
     `state["<name>"]`, or `"<name>" in state`.
  4. decide() WRITES EXACTLY the declared output names — verified behaviourally
     by running decide() on the empty state and on every committed sample and
     asserting the set of top-level keys in result["output"] equals the set of
     declared output names on every run.

Exit 0 = ports.json conforms; non-zero = a violation (prints the reason).
This is the connection-standard gate; keep it green.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent


def _fail(msg: str) -> None:
    print(f"check_ports: FAIL — {msg}", file=sys.stderr)
    raise SystemExit(1)


def _load_json(name: str) -> dict:
    p = HERE / name
    if not p.exists():
        _fail(f"{name} not found")
    try:
        return json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        _fail(f"{name} does not parse as JSON: {e}")
    return {}


def main() -> int:
    ports = _load_json("ports.json")
    types_doc = _load_json("types.json")

    vocab = types_doc.get("types")
    if not isinstance(vocab, dict) or not vocab:
        _fail("types.json has no non-empty 'types' object")
    vocab_names = set(vocab.keys())

    # ---- 1. shape ---------------------------------------------------------
    if not isinstance(ports.get("inputs"), list):
        _fail("ports.json 'inputs' must be a list")
    if not isinstance(ports.get("outputs"), list):
        _fail("ports.json 'outputs' must be a list")

    declared_inputs: list[str] = []
    declared_input_required: dict[str, bool] = {}
    for i, entry in enumerate(ports["inputs"]):
        if not isinstance(entry, dict):
            _fail(f"inputs[{i}] is not an object")
        name = entry.get("name")
        typ = entry.get("type")
        req = entry.get("required")
        if not isinstance(name, str) or not name:
            _fail(f"inputs[{i}] missing string 'name'")
        if not isinstance(typ, str) or not typ:
            _fail(f"input '{name}' missing string 'type'")
        if not isinstance(req, bool):
            _fail(f"input '{name}' missing boolean 'required'")
        # ---- 2. type in vocabulary ----
        if typ not in vocab_names:
            _fail(f"input '{name}' declares type '{typ}' not in types.json "
                  f"vocabulary {sorted(vocab_names)}")
        declared_inputs.append(name)
        declared_input_required[name] = req

    declared_outputs: list[str] = []
    for i, entry in enumerate(ports["outputs"]):
        if not isinstance(entry, dict):
            _fail(f"outputs[{i}] is not an object")
        name = entry.get("name")
        typ = entry.get("type")
        if not isinstance(name, str) or not name:
            _fail(f"outputs[{i}] missing string 'name'")
        if not isinstance(typ, str) or not typ:
            _fail(f"output '{name}' missing string 'type'")
        if typ not in vocab_names:
            _fail(f"output '{name}' declares type '{typ}' not in types.json "
                  f"vocabulary {sorted(vocab_names)}")
        declared_outputs.append(name)

    if not declared_outputs:
        _fail("ports.json declares no outputs")

    # ---- 3. decide() reads every declared input from state ---------------
    src = (HERE / "organ.py").read_text()
    for name in declared_inputs:
        patterns = (
            rf'state\.get\(\s*["\']{re.escape(name)}["\']',
            rf'state\[\s*["\']{re.escape(name)}["\']\s*\]',
            rf'["\']{re.escape(name)}["\']\s+in\s+state',
        )
        if not any(re.search(p, src) for p in patterns):
            _fail(f"organ.py does not read declared input '{name}' from state")

    # ---- 4. decide() writes EXACTLY the declared output names ------------
    sys.path.insert(0, str(HERE))
    from organ import decide  # noqa: E402

    declared_output_set = set(declared_outputs)
    runs: list[tuple[str, dict]] = [("empty_state", {})]
    sample_dir = HERE / "samples"
    if sample_dir.is_dir():
        for s in sorted(sample_dir.glob("*.json")):
            payload = json.loads(s.read_text())
            runs.append((s.name, payload.get("state", {})))

    for label, state in runs:
        result = decide(state, None)
        out = result.get("output")
        if not isinstance(out, dict):
            _fail(f"decide() on {label} did not return an 'output' object")
        got = set(out.keys())
        if got != declared_output_set:
            missing = declared_output_set - got
            extra = got - declared_output_set
            _fail(f"decide() on {label} output keys {sorted(got)} != declared "
                  f"{sorted(declared_output_set)} (missing={sorted(missing)}, "
                  f"extra={sorted(extra)})")

    print(f"check_ports: OK — {len(declared_inputs)} inputs, "
          f"{len(declared_outputs)} outputs, all types in vocabulary, "
          f"reads+writes verified across {len(runs)} runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
