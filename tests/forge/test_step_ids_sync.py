"""Guard: the forge dashboard's step-id list must stay in sync with the single
source of truth (orchestrator.STEPS / orchestrator.ALL_STEP_IDS).

Adding or reordering a Step in the orchestrator without updating the dashboard
(or vice versa) fails here loudly, instead of silently drifting the dashboard's
progress-bar denominator and "stuck in planning" detection. This is the
machine-enforced contract that lets new steps (e.g. the Phase-1 1A requirement
tribunal) be inserted into STEPS without quietly breaking forge observability.
"""
import importlib.util
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FASTSHIP_DIR = os.path.join(ROOT, "skills", "fastship")
FORGE_DIR = os.path.join(ROOT, "skills", "forge")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# orchestrator does a bare `import fastship_state`, so its dir must be importable.
if FASTSHIP_DIR not in sys.path:
    sys.path.insert(0, FASTSHIP_DIR)
orch = _load("orchestrator", os.path.join(FASTSHIP_DIR, "orchestrator.py"))
fd = _load("forge_dashboard", os.path.join(FORGE_DIR, "forge_dashboard.py"))
fg = _load("forge_gate", os.path.join(FORGE_DIR, "hooks", "forge_gate.py"))


class StepIdsInSyncTest(unittest.TestCase):
    def test_canonical_matches_steps(self):
        # ALL_STEP_IDS is exactly the ordered ids of STEPS — no second copy.
        self.assertEqual(orch.ALL_STEP_IDS, [s.id for s in orch.STEPS])

    def test_dashboard_python_list_in_sync(self):
        self.assertEqual(
            fd.ALL_STEPS, orch.ALL_STEP_IDS,
            "forge_dashboard.ALL_STEPS drifted from orchestrator.STEPS — "
            "update skills/forge/forge_dashboard.py ALL_STEPS to match.",
        )

    def test_dashboard_embedded_js_in_sync(self):
        # render_html injects ALL_STEPS into `const ALL=...`; prove the injection
        # happened and the JS list equals the Python list.
        html = fd.render_html()
        self.assertNotIn("__ALL_STEPS__", html, "step-id placeholder was not injected")
        m = re.search(r"const ALL=(\[[^\]]*\]);", html)
        self.assertIsNotNone(m, "could not find `const ALL=[...]` in dashboard HTML")
        self.assertEqual(
            json.loads(m.group(1)), fd.ALL_STEPS,
            "embedded JS step list out of sync with ALL_STEPS injection",
        )


class ForkDisciplineParityTest(unittest.TestCase):
    """Guard: the forge gate's fork interpretation (whether a plan's exclusive_forks
    waive the 1.5 grill) must agree with the orchestrator's _check_exclusive_forks for
    EVERY shape. Two implementations of fork-discipline silently drifted once (codex
    found the forge gate waiving the grill on a malformed fork the engine rejects);
    this pins them: 'requires grill' == 'open OR malformed' per the engine."""

    CASES = [
        [],                                                                  # none
        [{"id": "a", "decision": "d", "status": "resolved", "resolution": "r"}],  # all resolved
        [{"id": "a", "decision": "d", "status": "open"}],                    # open
        [{"id": "a", "decision": "d", "status": "resolved"}],                # resolved, no resolution
        [{"id": "a", "decision": "d", "status": "typo"}],                    # bad status
        [{"decision": "d", "status": "open"}],                               # missing id
        [{"id": "  ", "decision": "d", "status": "open"}],                   # blank id
        [{"id": "a", "decision": "", "status": "open"}],                     # blank decision
        [{"id": "a", "decision": "d", "status": "open"},
         {"id": "a", "decision": "e", "status": "resolved", "resolution": "x"}],  # dup id
        [42],                                                                # non-dict entry
        "not-a-list",                                                        # wrong type
        [{"id": "a", "decision": "d", "status": "resolved", "resolution": "r"},
         {"id": "b", "decision": "e", "status": "open"}],                    # mixed → open
    ]

    def test_forge_gate_fork_discipline_matches_orchestrator(self):
        for forks in self.CASES:
            ok, _msg, open_ids = orch._check_exclusive_forks(forks)
            engine_requires_grill = (not ok) or bool(open_ids)
            self.assertEqual(
                fg._forks_require_grill(forks), engine_requires_grill,
                f"forge_gate._forks_require_grill drifted from orchestrator "
                f"_check_exclusive_forks for forks={forks!r}",
            )


if __name__ == "__main__":
    unittest.main()
