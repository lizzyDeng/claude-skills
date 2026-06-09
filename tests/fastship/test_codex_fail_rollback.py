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
    base = {"gate": "FAIL", "p0_requirements_missing": []}
    base.update(fields)
    return "## Review\n```json\n" + json.dumps(base, ensure_ascii=False) + "\n```\n### GATE: FAIL\n"


# ── routing: _codex_fail_rollback_step ──────────────────────────────────────

def test_requirements_layer_routes_to_1_3r():
    from orchestrator import _codex_fail_rollback_step
    content = _gate_block(p0_requirements_missing=["改名审核需求漏了"])
    assert _codex_fail_rollback_step({"request_type": "feature"}, content) == "1.3r"


def test_plan_layer_routes_to_1_4():
    from orchestrator import _codex_fail_rollback_step
    content = _gate_block(p0_requirements_missing=[], uncovered_ac=["ac-2"])
    assert _codex_fail_rollback_step({"request_type": "feature"}, content) == "1.4"


def test_bugfix_always_routes_to_1_4():
    # bugfix has no 1A; even a requirements-layer flag can't rewind to a skipped 1.3r.
    from orchestrator import _codex_fail_rollback_step
    content = _gate_block(p0_requirements_missing=["x"])
    assert _codex_fail_rollback_step({"request_type": "bugfix"}, content) == "1.4"


def test_unparseable_defaults_to_1_4():
    from orchestrator import _codex_fail_rollback_step
    assert _codex_fail_rollback_step({"request_type": "feature"}, "no json at all") == "1.4"
    assert _codex_fail_rollback_step({"request_type": "feature"}, "```json\n{bad json\n```") == "1.4"
    assert _codex_fail_rollback_step({"request_type": "feature"}, "") == "1.4"


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
