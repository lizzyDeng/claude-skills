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


def _full_gate(**over):
    g = {"gate": "FAIL", "reviewed_plan_sha256": "x", "p0_contract_reviewed": True,
         "ac_e2e_coverage_reviewed": True, "weak_case_reviewed": True,
         "evidence_plan_reviewed": True, "p0_requirements_missing": [], "uncovered_ac": [],
         "unmapped_e2e_scenarios": [], "weak_scenarios": [], "non_business_assertions": [],
         "missing_evidence": []}
    g.update(over)
    return g


class CodexGateSelectorParityTest(unittest.TestCase):
    """The forge gate and the orchestrator must identify the codex 1.5c CONTRACT gate
    the SAME way — else a trailing example block could be read as the gate by one and not
    the other (the F7-misroute seam). Pins forge._extract_codex_review_gate to the
    orchestrator's for every shape."""

    CONTENTS = [
        "## R\n```json\n" + json.dumps(_full_gate()) + "\n```\n",                       # one full gate
        "## R\n```json\n" + json.dumps(_full_gate(p0_requirements_missing=["x"])) + "\n```\n"
        + "```json\n" + json.dumps({"gate": "example-only"}) + "\n```\n",                # + bogus gate key
        "## R\n```json\n" + json.dumps(_full_gate()) + "\n```\n"
        + "```json\n" + json.dumps({"unrelated": True}) + "\n```\n",                     # + unrelated
        "## R\n```json\n" + json.dumps({"gate": "PASS"}) + "\n```\n",                    # gate but no arrays
        "## R\nno json here\n",                                                          # none
        "## R\n```json\n{bad json\n```\n",                                              # unparseable
    ]

    def test_gate_selector_matches(self):
        for content in self.CONTENTS:
            self.assertEqual(
                fg._extract_codex_review_gate(content),
                orch._extract_codex_review_gate(content),
                f"codex gate selector drifted for content={content!r}",
            )


class GrillResolutionParityTest(unittest.TestCase):
    """Forge G2's grill fork-resolution verdict must equal the orchestrator's
    _check_grill_fork_resolution for every shape (codex review [P2]: forge was laxer).
    Pins the two so a grill the engine rejects can't pass G2."""

    OPEN = {"tf-1", "tf-2"}
    GATES = [
        {"fork_resolutions": [{"id": "tf-1", "resolution": "a"}, {"id": "tf-2", "resolution": "b"}]},  # ok
        {"fork_resolutions": []},                                                       # empty
        {"other": 1},                                                                   # missing field
        {"fork_resolutions": [{"id": "tf-1", "resolution": "a"}]},                       # tf-2 unresolved
        {"fork_resolutions": [{"id": "tf-1", "resolution": "a"}, {"id": "tf-2", "resolution": "b"},
                              {"id": "tf-ghost", "resolution": "c"}]},                   # dangling
        {"fork_resolutions": [{"id": "tf-1", "resolution": "a"}, {"id": "tf-1", "resolution": "b"},
                              {"id": "tf-2", "resolution": "c"}]},                       # duplicate
        {"fork_resolutions": [{"id": "tf-1", "resolution": "  "}, {"id": "tf-2", "resolution": "b"}]},  # blank res
        {"fork_resolutions": [{"id": "  ", "resolution": "a"}]},                         # blank id
        {"fork_resolutions": ["tf-1"]},                                                 # non-object entry
    ]

    def test_grill_resolution_verdict_matches(self):
        for gate in self.GATES:
            self.assertEqual(
                fg._check_grill_fork_resolution(set(self.OPEN), gate)[0],
                orch._check_grill_fork_resolution(set(self.OPEN), gate)[0],
                f"grill fork-resolution verdict drifted for gate={gate!r}",
            )


if __name__ == "__main__":
    unittest.main()
