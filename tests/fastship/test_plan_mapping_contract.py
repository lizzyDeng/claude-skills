"""Unit tests for the 1B 技术方案 AC→task+E2E mapping contract (step 1.4, feature).

The pure checker (_check_plan_mapping) is the engine-enforced heart of the Phase-1
1B redesign: the technical plan must map EVERY locked 1A P0 AC to ≥1 implementation
task AND ≥1 E2E scenario, by reference to a real AC id. A missing / dangling /
duplicated / uncovered mapping FAILs on the spot — it does not wait for codex.

These mirror the 1A discipline (dup-id, no-invention/dangling, no-drop coverage),
which codex proved are the exact bypasses a green test suite otherwise misses.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship'))


def check(p0_ac_ids, plan_gate):
    from orchestrator import _check_plan_mapping
    return _check_plan_mapping(set(p0_ac_ids), plan_gate)


def valid_mapping():
    return {"ac_mapping": [
        {"ac_id": "ac-1", "tasks": ["实现改名 API"], "e2e": ["E2E-rename-persists"]},
        {"ac_id": "ac-2", "tasks": ["前端改名表单"], "e2e": ["E2E-rename-ui"]},
    ]}


P0 = ["ac-1", "ac-2"]


# ── pure discipline checks ──────────────────────────────────────────────────

def test_valid_mapping_passes():
    ok, msg = check(P0, valid_mapping())
    assert ok, msg


def test_non_dict_plan_gate_fails():
    ok, _ = check(P0, ["not", "a", "dict"])
    assert ok is False


def test_missing_ac_mapping_fails():
    ok, msg = check(P0, {"something_else": []})
    assert ok is False and "ac_mapping" in msg


def test_empty_ac_mapping_fails():
    ok, msg = check(P0, {"ac_mapping": []})
    assert ok is False and "ac_mapping" in msg


def test_mapping_entry_not_object_fails():
    ok, msg = check(P0, {"ac_mapping": ["ac-1"]})
    assert ok is False and "object" in msg


def test_mapping_blank_ac_id_fails():
    g = valid_mapping()
    g["ac_mapping"][0]["ac_id"] = "  "
    ok, msg = check(P0, g)
    assert ok is False and "ac_id" in msg


def test_dangling_ac_id_fails():
    # Referencing an AC id absent from the trusted 1A ACs = fabricated coverage.
    g = valid_mapping()
    g["ac_mapping"][0]["ac_id"] = "ac-ghost"
    ok, msg = check(P0, g)
    assert ok is False and "ac-ghost" in msg and "dangling" in msg


def test_duplicate_ac_id_fails():
    # Two entries for the same AC would let the coverage diff hide a real gap.
    g = valid_mapping()
    g["ac_mapping"].append({"ac_id": "ac-1", "tasks": ["dup"], "e2e": ["E2E-dup"]})
    ok, msg = check(P0, g)
    assert ok is False and "重复" in msg


def test_empty_tasks_fails():
    g = valid_mapping()
    g["ac_mapping"][0]["tasks"] = []
    ok, msg = check(P0, g)
    assert ok is False and "tasks" in msg


def test_empty_e2e_fails():
    g = valid_mapping()
    g["ac_mapping"][1]["e2e"] = []
    ok, msg = check(P0, g)
    assert ok is False and "e2e" in msg


def test_blank_task_entry_fails():
    g = valid_mapping()
    g["ac_mapping"][0]["tasks"] = ["  "]
    ok, msg = check(P0, g)
    assert ok is False and "tasks" in msg


def test_uncovered_ac_fails():
    # The core invariant: a P0 AC with no mapping entry at all → 1B FAIL.
    g = {"ac_mapping": [{"ac_id": "ac-1", "tasks": ["t"], "e2e": ["E2E-x"]}]}
    ok, msg = check(P0, g)
    assert ok is False and "ac-2" in msg and "未在技术方案中映射" in msg


# ── _collect_required_ac_ids / _extract_plan_mapping_gate ───────────────────

def test_collect_required_ac_ids_p0_only():
    from orchestrator import _collect_required_ac_ids
    gate = {"p0": [
        {"id": "p0-1", "observable_ac": [{"id": "ac-1", "assertion": "a"},
                                         {"id": "ac-2", "assertion": "b"}]},
        {"id": "p0-2", "observable_ac": [{"id": "ac-3", "assertion": "c"}]},
    ]}
    assert _collect_required_ac_ids(gate) == {"ac-1", "ac-2", "ac-3"}


def test_collect_required_ac_ids_includes_p1():
    # P1 ACs join the universe 1B must cover (P1 is a hard mapping gate too).
    from orchestrator import _collect_required_ac_ids
    gate = {
        "p0": [{"id": "p0-1", "observable_ac": [{"id": "ac-1", "assertion": "a"}]}],
        "p1": [{"id": "p1-1", "observable_ac": [{"id": "ac-2", "assertion": "b"},
                                                {"id": "ac-3", "assertion": "c"}]}],
    }
    assert _collect_required_ac_ids(gate) == {"ac-1", "ac-2", "ac-3"}


def test_extract_plan_mapping_gate_picks_the_ac_mapping_block():
    from orchestrator import _extract_plan_mapping_gate
    plan = (
        "# Plan\n```mermaid\nflowchart TD\nA-->B\n```\n"
        '```json\n{"unrelated": true}\n```\n'
        '```json\n{"ac_mapping": [{"ac_id": "ac-1", "tasks": ["t"], "e2e": ["E2E-x"]}]}\n```\n'
    )
    gate = _extract_plan_mapping_gate(plan)
    assert gate is not None and "ac_mapping" in gate
    assert gate["ac_mapping"][0]["ac_id"] == "ac-1"


def test_extract_plan_mapping_gate_none_when_absent():
    from orchestrator import _extract_plan_mapping_gate
    assert _extract_plan_mapping_gate("# Plan\nno json here at all\n") is None


# ── validate_plan end-to-end (feature requires mapping; bugfix skips) ───────

def _signed_plan(body_json=None):
    base = ("# Plan\n> **For agentic workers:** REQUIRED\n"
            "**Goal:** rename\n- [ ] **Step 1:** test\n")
    if body_json is not None:
        base += "## AC→task+E2E\n```json\n" + json.dumps(body_json, ensure_ascii=False) + "\n```\n"
    return base


def _requirements_md():
    gate = {
        "roles": [
            {"role": "产品", "abstain": False, "concerns": [
                {"id": "c1", "kind": "ac", "point": "改名", "evidence_ref": "用户原话"}]},
            {"role": "运营", "abstain": True, "concerns": []},
            {"role": "数据", "abstain": True, "concerns": []},
            {"role": "财务", "abstain": True, "concerns": []},
        ],
        "additive_union": [{"id": "c1", "kind": "ac", "point": "改名", "sources": ["产品"]}],
        "exclusive_forks": [],
        "p0": [{"id": "p0-1", "source": "用户原话",
                "observable_ac": [{"id": "ac-1", "assertion": "改名后昵称更新"}]}],
    }
    return "# 需求定稿\n## 契约\n```json\n" + json.dumps(gate, ensure_ascii=False) + "\n```\n" + "占位 " * 30


def _requirements_md_with_p1():
    gate = {
        "roles": [
            {"role": "产品", "abstain": False, "concerns": [
                {"id": "c1", "kind": "ac", "point": "改名", "evidence_ref": "用户原话"}]},
            {"role": "运营", "abstain": True, "concerns": []},
            {"role": "数据", "abstain": True, "concerns": []},
            {"role": "财务", "abstain": True, "concerns": []},
        ],
        "additive_union": [{"id": "c1", "kind": "ac", "point": "改名", "sources": ["产品"]}],
        "exclusive_forks": [],
        "p0": [{"id": "p0-1", "source": "用户原话",
                "observable_ac": [{"id": "ac-1", "assertion": "改名后昵称更新"}]}],
        "p1": [{"id": "p1-1", "source": "brief.md:9",
                "observable_ac": [{"id": "ac-2", "assertion": "改名记入操作日志"}]}],
    }
    return "# 需求定稿\n## 契约\n```json\n" + json.dumps(gate, ensure_ascii=False) + "\n```\n" + "占位 " * 30


def _setup(tmp_path, monkeypatch, plan_body, request_type="feature", seed_1a=True, req_md=None):
    import orchestrator as o
    monkeypatch.setattr(o, "_repo_root", lambda: str(tmp_path))
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    plan_dir = tmp_path / "docs" / "superpowers" / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan = plan_dir / "2026-06-08-rename.md"
    plan.write_text(plan_body)
    orch = {"plan_path": str(plan), "artifacts": {}, "request_type": request_type}
    ok, _ = o.record_step_artifact(orch, "1.4", str(plan), source="test")
    assert ok
    if seed_1a:
        req = claude / ".fastship-requirements.md"
        req.write_text(req_md if req_md is not None else _requirements_md())
        ok, _ = o.record_step_artifact(orch, o.REQUIREMENTS_STEP_ID, str(req), source="test")
        assert ok
        orch["artifacts"]["requirements_path"] = str(req)
    return o, orch, plan


def test_validate_plan_feature_passes_with_full_mapping(tmp_path, monkeypatch):
    o, orch, _ = _setup(
        tmp_path, monkeypatch,
        _signed_plan({"ac_mapping": [{"ac_id": "ac-1", "tasks": ["改名 API"], "e2e": ["E2E-rename"]}]}))
    ok, msg = o.validate_plan(orch, {})
    assert ok, msg


def test_validate_plan_feature_fails_dangling_ac(tmp_path, monkeypatch):
    # signed plan maps a fabricated AC id; the real P0 AC (ac-1) is left uncovered.
    # The dangling reference is caught first (技术方案 can't invent coverage).
    o, orch, _ = _setup(
        tmp_path, monkeypatch,
        _signed_plan({"ac_mapping": [{"ac_id": "ac-other", "tasks": ["x"], "e2e": ["E2E-x"]}]}))
    ok, msg = o.validate_plan(orch, {})
    assert ok is False and "ac-other" in msg


def test_validate_plan_feature_fails_missing_mapping_block(tmp_path, monkeypatch):
    o, orch, _ = _setup(tmp_path, monkeypatch, _signed_plan(None))  # signed, no json block
    ok, msg = o.validate_plan(orch, {})
    assert ok is False and "ac_mapping" in msg


def test_validate_plan_feature_fails_without_1a_requirements(tmp_path, monkeypatch):
    o, orch, _ = _setup(
        tmp_path, monkeypatch,
        _signed_plan({"ac_mapping": [{"ac_id": "ac-1", "tasks": ["t"], "e2e": ["E2E-x"]}]}),
        seed_1a=False)
    ok, msg = o.validate_plan(orch, {})
    assert ok is False and "requirements_path" in msg


def test_validate_plan_bugfix_skips_mapping(tmp_path, monkeypatch):
    # bugfix: no 1A, signed plan with no mapping block still passes (signature-only).
    o, orch, _ = _setup(tmp_path, monkeypatch, _signed_plan(None),
                        request_type="bugfix", seed_1a=False)
    ok, msg = o.validate_plan(orch, {})
    assert ok, msg


def test_validate_plan_feature_fails_uncovered_p1_ac(tmp_path, monkeypatch):
    # P1 is a hard mapping gate too: plan covers p0 (ac-1) but leaves p1 (ac-2) unmapped → FAIL.
    o, orch, _ = _setup(
        tmp_path, monkeypatch,
        _signed_plan({"ac_mapping": [{"ac_id": "ac-1", "tasks": ["改名 API"], "e2e": ["E2E-rename"]}]}),
        req_md=_requirements_md_with_p1())
    ok, msg = o.validate_plan(orch, {})
    assert ok is False and "ac-2" in msg


def test_validate_plan_feature_passes_with_p0_and_p1_mapping(tmp_path, monkeypatch):
    # Mapping both the p0 AC and the p1 AC passes — P1 is covered, not waived.
    o, orch, _ = _setup(
        tmp_path, monkeypatch,
        _signed_plan({"ac_mapping": [
            {"ac_id": "ac-1", "tasks": ["改名 API"], "e2e": ["E2E-rename"]},
            {"ac_id": "ac-2", "tasks": ["写操作日志"], "e2e": ["E2E-rename-audit"]},
        ]}),
        req_md=_requirements_md_with_p1())
    ok, msg = o.validate_plan(orch, {})
    assert ok, msg


# ── F4: 1B technical forks gate the 1.5 grill ──────────────────────────────

def _forks(forks):
    from orchestrator import _check_exclusive_forks
    return _check_exclusive_forks(forks)


def test_check_exclusive_forks_valid_returns_open_ids():
    ok, msg, open_ids = _forks([
        {"id": "tf-1", "decision": "PG vs Redis", "status": "open"},
        {"id": "tf-2", "decision": "REST vs RPC", "status": "resolved", "resolution": "REST"},
    ])
    assert ok and open_ids == ["tf-1"]


def test_check_exclusive_forks_empty_ok():
    ok, _, open_ids = _forks([])
    assert ok and open_ids == []


def test_check_exclusive_forks_non_list_fails():
    ok, msg, _ = _forks({"id": "x"})
    assert ok is False and "数组" in msg


def test_check_exclusive_forks_dup_id_fails():
    ok, msg, _ = _forks([
        {"id": "tf-1", "decision": "a", "status": "open"},
        {"id": "tf-1", "decision": "b", "status": "open"},
    ])
    assert ok is False and "重复" in msg


def test_check_exclusive_forks_bad_status_fails():
    ok, msg, _ = _forks([{"id": "tf-1", "decision": "a", "status": "maybe"}])
    assert ok is False and "status" in msg


def test_check_exclusive_forks_resolved_without_resolution_fails():
    ok, msg, _ = _forks([{"id": "tf-1", "decision": "a", "status": "resolved"}])
    assert ok is False and "resolution" in msg


def test_validate_plan_stashes_open_forks(tmp_path, monkeypatch):
    o, orch, _ = _setup(tmp_path, monkeypatch, _signed_plan({
        "ac_mapping": [{"ac_id": "ac-1", "tasks": ["t"], "e2e": ["E2E-x"]}],
        "exclusive_forks": [{"id": "tf-1", "decision": "存哪", "status": "open"}],
    }))
    ok, msg = o.validate_plan(orch, {})
    assert ok, msg
    assert orch["artifacts"]["plan_open_fork_ids"] == ["tf-1"]


def test_validate_plan_stashes_empty_when_no_fork(tmp_path, monkeypatch):
    o, orch, _ = _setup(tmp_path, monkeypatch, _signed_plan({
        "ac_mapping": [{"ac_id": "ac-1", "tasks": ["t"], "e2e": ["E2E-x"]}],
    }))
    ok, msg = o.validate_plan(orch, {})
    assert ok, msg
    assert orch["artifacts"]["plan_open_fork_ids"] == []


def test_validate_plan_rejects_malformed_fork(tmp_path, monkeypatch):
    o, orch, _ = _setup(tmp_path, monkeypatch, _signed_plan({
        "ac_mapping": [{"ac_id": "ac-1", "tasks": ["t"], "e2e": ["E2E-x"]}],
        "exclusive_forks": [{"id": "tf-1", "decision": "存哪", "status": "huh"}],
    }))
    ok, msg = o.validate_plan(orch, {})
    assert ok is False and "exclusive_forks" in msg


# ── F4: _advance_state 1.5 skip/run ─────────────────────────────────────────

def _adv_from_1_4(request_type, open_fork_ids):
    import orchestrator as o
    orch = {"current_step": "1.4", "request_type": request_type,
            "completed_steps": [], "skipped_steps": [], "phase": 1, "artifacts": {}}
    if open_fork_ids is not None:
        orch["artifacts"]["plan_open_fork_ids"] = open_fork_ids
    return o._advance_state(orch)


def test_feature_no_open_fork_skips_grill():
    orch = _adv_from_1_4("feature", [])
    assert orch["current_step"] == "1.5c"          # grill auto-skipped
    assert "1.5" in orch["skipped_steps"]


def test_feature_absent_signal_skips_grill():
    # defensive: no stashed signal == no open fork → skip (validate_plan always stashes).
    orch = _adv_from_1_4("feature", None)
    assert orch["current_step"] == "1.5c"
    assert "1.5" in orch["skipped_steps"]


def test_feature_open_fork_runs_grill():
    orch = _adv_from_1_4("feature", ["tf-1"])
    assert orch["current_step"] == "1.5"           # human arbitrates the open fork
    assert "1.5" not in orch["skipped_steps"]


def test_bugfix_always_runs_grill():
    orch = _adv_from_1_4("bugfix", None)
    assert orch["current_step"] == "1.5"           # bugfix keeps its plan grill
    assert "1.5" not in orch["skipped_steps"]
