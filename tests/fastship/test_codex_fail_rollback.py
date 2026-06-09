"""Unit tests for F7 — codex-review FAIL rewinds by DEFECT LAYER.

需求层 (codex flags p0_requirements_missing) → 1.3r so the 1A tribunal re-derives the
requirement; 方案层 (coverage/quality gaps) → 1.4. bugfix has no 1A → always 1.4.
Parse failure is fail-closed to 1.4 (least re-work; the loop cap backstops a mis-route).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship'))


def _gate_block(**fields):
    import json
    # A full 1.5c contract gate (all coverage arrays present) — the strict
    # _extract_codex_review_gate only recognizes a block carrying every array.
    base = {"gate": "FAIL", "p0_requirements_missing": [], "uncovered_ac": [],
            "unmapped_e2e_scenarios": [], "weak_scenarios": [],
            "non_business_assertions": [], "missing_evidence": []}
    base.update(fields)
    return "## Review\n```json\n" + json.dumps(base, ensure_ascii=False) + "\n```\n### GATE: FAIL\n"


# ── routing: _codex_fail_rollback_step ──────────────────────────────────────
# 需求层 routing is gated on a TRUSTED 1.3r artifact existing (1A actually ran), derived
# from trusted evidence rather than the mutable request_type field.

def _with_trusted_1a():
    return {"artifacts": {"trusted_artifacts": {"1.3r": {"sha256": "x"}}}}


def test_requirements_layer_routes_to_1_3r():
    from orchestrator import _codex_fail_rollback_step
    content = _gate_block(p0_requirements_missing=["改名审核需求漏了"])
    assert _codex_fail_rollback_step(_with_trusted_1a(), content) == "1.3r"


def test_plan_layer_routes_to_1_4():
    from orchestrator import _codex_fail_rollback_step
    content = _gate_block(p0_requirements_missing=[], uncovered_ac=["ac-2"])
    assert _codex_fail_rollback_step(_with_trusted_1a(), content) == "1.4"


def test_requirements_layer_without_trusted_1a_falls_to_1_4():
    # Derive from trusted evidence, NOT request_type: no trusted 1.3r → nothing to rewind
    # to → 1.4. A bugfix (1.3r skipped) lands here; so would a tampered/absent 1A lock.
    from orchestrator import _codex_fail_rollback_step
    content = _gate_block(p0_requirements_missing=["x"])
    assert _codex_fail_rollback_step({"artifacts": {}}, content) == "1.4"
    # flipping request_type alone must not buy a rewind to 1.3r…
    assert _codex_fail_rollback_step({"request_type": "feature", "artifacts": {}}, content) == "1.4"
    # …nor avoid it: a trusted 1A present routes to 1.3r even labeled bugfix.
    orch = {"request_type": "bugfix", "artifacts": {"trusted_artifacts": {"1.3r": {"sha256": "x"}}}}
    assert _codex_fail_rollback_step(orch, content) == "1.3r"


def test_trailing_json_block_does_not_misroute():
    # The gate is the full CONTRACT block (gate∈PASS/FAIL + coverage arrays), not merely
    # the last json block — neither a trailing unrelated block NOR a trailing block with a
    # bogus `gate` key ({"gate":"example-only"}) may hide p0_requirements_missing and
    # misroute 需求层 → 1.4 (codex review round-2 residual).
    import json
    from orchestrator import _codex_fail_rollback_step
    content = (_gate_block(p0_requirements_missing=["missing audit"])
               + "\n附录\n```json\n" + json.dumps({"unrelated": True}) + "\n```\n"
               + "示例\n```json\n" + json.dumps({"gate": "example-only"}) + "\n```\n")
    assert _codex_fail_rollback_step(_with_trusted_1a(), content) == "1.3r"


def test_trailing_full_pass_template_does_not_flip_routing():
    # codex round-3: a real FAIL gate (with p0_requirements_missing) followed by a COMPLETE
    # PASS contract template carrying its own `### GATE: PASS` must not flip routing — the
    # gate binds to the FIRST `### GATE:` verdict (FAIL), ignoring blocks after it.
    import json
    from orchestrator import _codex_fail_rollback_step
    real = _gate_block(p0_requirements_missing=["missing P0"])          # ...### GATE: FAIL
    fake_pass_gate = {"gate": "PASS", "p0_requirements_missing": [], "uncovered_ac": [],
                      "unmapped_e2e_scenarios": [], "weak_scenarios": [],
                      "non_business_assertions": [], "missing_evidence": []}
    fake = ("### Contract Gate\n```json\n" + json.dumps(fake_pass_gate)
            + "\n```\n### GATE: PASS\n")
    assert _codex_fail_rollback_step(_with_trusted_1a(), real + fake) == "1.3r"


def test_unparseable_defaults_to_1_4():
    from orchestrator import _codex_fail_rollback_step
    a = _with_trusted_1a()
    assert _codex_fail_rollback_step(a, "no json at all") == "1.4"
    assert _codex_fail_rollback_step(a, "```json\n{bad json\n```") == "1.4"
    assert _codex_fail_rollback_step(a, "") == "1.4"


# ── state mutation: _apply_codex_fail_rollback ──────────────────────────────

def _orch():
    return {
        "request_type": "feature", "plan_path": "/p", "current_step": "1.5c", "phase": 1,
        "completed_steps": ["1.0", "1.3r", "1.4", "1.5c"],
        "skipped_steps": ["1.3d", "1.5"],
        "artifacts": {
            "requirements_path": "/r", "grill_result_path": "/g", "codex_review_path": "/c",
            "plan_open_fork_ids": [],
            "trusted_artifacts": {"1.3r": {"sha256": "a"}, "1.4": {"sha256": "b"},
                                  "1.5c": {"sha256": "c"}},
        },
    }


def test_apply_rollback_to_plan_preserves_1a():
    from orchestrator import _apply_codex_fail_rollback
    orch = _orch()
    _apply_codex_fail_rollback(orch, "1.4")
    assert orch["current_step"] == "1.4" and orch["phase"] == 1
    assert orch["plan_path"] is None
    assert "1.4" not in orch["completed_steps"] and "1.5c" not in orch["completed_steps"]
    assert "1.3r" in orch["completed_steps"]            # 1A lock kept
    assert "1.5" not in orch["skipped_steps"]           # stale auto-skip cleared
    for k in ("grill_result_path", "codex_review_path", "plan_open_fork_ids"):
        assert k not in orch["artifacts"]
    assert orch["artifacts"]["requirements_path"] == "/r"            # preserved
    ta = orch["artifacts"]["trusted_artifacts"]
    assert "1.3r" in ta and "1.4" not in ta and "1.5c" not in ta


def test_apply_rollback_to_requirements_resets_1a():
    from orchestrator import _apply_codex_fail_rollback
    orch = _orch()
    _apply_codex_fail_rollback(orch, "1.3r")
    assert orch["current_step"] == "1.3r"
    assert "requirements_path" not in orch["artifacts"]              # 1A lock reset
    assert "1.3r" not in orch["completed_steps"]
    assert "1.3r" not in orch["artifacts"]["trusted_artifacts"]
    assert "1.4" not in orch["artifacts"]["trusted_artifacts"]


def test_apply_rollback_is_idempotent():
    from orchestrator import _apply_codex_fail_rollback
    orch = _orch()
    _apply_codex_fail_rollback(orch, "1.4")
    snapshot = dict(orch)
    _apply_codex_fail_rollback(orch, "1.4")   # re-applying must not corrupt state
    assert orch["current_step"] == snapshot["current_step"]
    assert orch["completed_steps"] == snapshot["completed_steps"]
