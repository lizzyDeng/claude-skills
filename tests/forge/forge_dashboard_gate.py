#!/usr/bin/env python3
"""E2E gate for the forge dashboard. Validates an e2e_runner result: enough
turns + every per-turn assertion (expect_ok) true + top-level passed flag.
Prints raw turns so the verdict is not self-certified by the runner.

Distinct from the project's generic tests/e2e_gate.py (different schema): this
gate reads the flat {turns:[{expect_ok}], passed} shape forge_dashboard_e2e.py
emits, and FAILS if any business assertion is false."""
import argparse, json, sys

ap = argparse.ArgumentParser()
ap.add_argument("--result", required=True)
ap.add_argument("--min-turns", type=int, default=8)
a = ap.parse_args()

with open(a.result) as f:
    r = json.load(f)
turns = r.get("turns", [])
fails = [t for t in turns if not t.get("expect_ok")]
print(f"scenario={r.get('scenario')} turns={len(turns)} min={a.min_turns} fails={len(fails)} passed={r.get('passed')}")
for t in turns:
    print(f"  {'OK ' if t.get('expect_ok') else 'FAIL'} #{t.get('turn')} {t.get('action')} -> {t.get('observation')}")
ok = (len(turns) >= a.min_turns) and (not fails) and (r.get("passed") is True)
print("GATE PASS" if ok else "GATE FAIL")
sys.exit(0 if ok else 1)
