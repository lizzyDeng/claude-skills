"""Unit tests for the 1A requirements-lock synthesis-discipline contract.

The pure checker (_check_requirements_contract) is the engine-enforced heart of
the Phase-1 1A redesign: the 书记员 is a clerk, not a judge. These tests pin each
discipline rule — especially "additive 并集不减", which is the exact failure mode
the multi-role dogfood tribunal exhibited (the synthesizer silently buried a
fork / dropped a concern). They run with no fixtures, files, or hashing.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship'))


def valid_gate():
    return {
        "stage": "1A-requirements",
        "roles": [
            {"role": "产品", "abstain": False, "concerns": [
                {"id": "pr-1", "kind": "ac", "point": "用户能改名", "evidence_ref": "用户原话: 我要改名"},
            ]},
            {"role": "数据", "abstain": False, "concerns": [
                {"id": "da-1", "kind": "risk", "point": "改名频率需限流", "evidence_ref": "brief.md:42"},
            ]},
            {"role": "财务", "abstain": True, "concerns": []},
            {"role": "运营", "abstain": True, "concerns": []},
        ],
        "additive_union": [
            {"id": "pr-1", "kind": "ac", "point": "用户能改名", "sources": ["产品"]},
            {"id": "da-1", "kind": "risk", "point": "改名频率需限流", "sources": ["数据"]},
        ],
        "exclusive_forks": [
            {"id": "f1", "decision": "改名是否需审核", "status": "resolved",
             "resolution": "需走敏感词过滤", "options": []},
        ],
        "p0": [
            {"id": "p0-1", "source": "用户原始需求",
             "observable_ac": [{"id": "ac-1", "assertion": "改名后昵称在 profile 更新"}]},
        ],
        "p1": [],
        "constraints": [],
        "open_questions": [],
    }


def check(gate):
    from orchestrator import _check_requirements_contract
    return _check_requirements_contract(gate)


# ── pure discipline checks ──────────────────────────────────────────────────

def test_valid_gate_passes():
    ok, msg = check(valid_gate())
    assert ok, msg


def test_non_dict_fails():
    ok, _ = check(["not", "a", "dict"])
    assert ok is False


def test_missing_required_list_field_fails():
    g = valid_gate()
    g.pop("roles")
    ok, msg = check(g)
    assert ok is False and "roles" in msg


def test_abstain_role_with_concerns_fails():
    g = valid_gate()
    g["roles"][2]["concerns"] = [{"id": "x", "kind": "risk", "point": "p", "evidence_ref": "e"}]
    ok, msg = check(g)
    assert ok is False and "弃权" in msg


def test_concern_missing_evidence_ref_fails():
    g = valid_gate()
    del g["roles"][0]["concerns"][0]["evidence_ref"]
    ok, msg = check(g)
    assert ok is False and "evidence_ref" in msg


def test_all_roles_abstain_fails():
    g = valid_gate()
    for r in g["roles"]:
        r["abstain"] = True
        r["concerns"] = []
    g["additive_union"] = []
    ok, msg = check(g)
    assert ok is False and "全部角色弃权" in msg


def test_additive_union_dropping_a_concern_fails():
    # The dogfood failure mode: synthesizer silently omits a role concern.
    g = valid_gate()
    g["additive_union"] = [u for u in g["additive_union"] if u["id"] != "da-1"]
    ok, msg = check(g)
    assert ok is False and "da-1" in msg and "并集" in msg


def test_additive_union_item_missing_sources_fails():
    g = valid_gate()
    g["additive_union"][0].pop("sources")
    ok, msg = check(g)
    assert ok is False and "sources" in msg


def test_open_fork_fails():
    g = valid_gate()
    g["exclusive_forks"][0]["status"] = "open"
    ok, msg = check(g)
    assert ok is False and "未裁决" in msg


def test_resolved_fork_without_resolution_fails():
    g = valid_gate()
    g["exclusive_forks"][0]["resolution"] = "   "
    ok, msg = check(g)
    assert ok is False and "resolution" in msg


def test_no_p0_fails():
    g = valid_gate()
    g["p0"] = []
    ok, msg = check(g)
    assert ok is False and "P0" in msg


def test_p0_without_ac_fails():
    g = valid_gate()
    g["p0"][0]["observable_ac"] = []
    ok, msg = check(g)
    assert ok is False and "observable_ac" in msg


def test_p0_without_source_fails():
    g = valid_gate()
    g["p0"][0]["source"] = ""
    ok, msg = check(g)
    assert ok is False and "source" in msg


# ── validate_requirements plumbing (ledger + JSON extraction) ───────────────

def requirements_md(gate):
    return (
        "# 需求定稿 (1A requirements-lock)\n\n"
        "## 角色拷打 + 合成\n本文件由 1A 多角色法庭产出,书记员机械合成,grill 合岔路。\n\n"
        "## 契约\n```json\n" + json.dumps(gate, ensure_ascii=False, indent=2) + "\n```\n"
    )


def _trust(orch, step_id, path):
    from orchestrator import record_step_artifact
    ok, msg = record_step_artifact(orch, step_id, str(path), source="test")
    assert ok, msg


def test_validate_requirements_pass(tmp_path, monkeypatch):
    import orchestrator as o
    monkeypatch.setattr(o, "_repo_root", lambda: str(tmp_path))
    p = tmp_path / ".claude" / ".fastship-requirements.md"
    p.parent.mkdir(parents=True)
    p.write_text(requirements_md(valid_gate()))
    orch = {"artifacts": {}}
    _trust(orch, o.REQUIREMENTS_STEP_ID, p)
    orch["artifacts"]["requirements_path"] = str(p)
    ok, msg = o.validate_requirements(orch, {})
    assert ok, msg


def test_validate_requirements_rejects_filesystem_fallback(tmp_path, monkeypatch):
    import orchestrator as o
    monkeypatch.setattr(o, "_repo_root", lambda: str(tmp_path))
    ok, msg = o.validate_requirements({"artifacts": {}}, {})
    assert ok is False and "fallback" in msg


def test_validate_requirements_rejects_no_json_block(tmp_path, monkeypatch):
    import orchestrator as o
    monkeypatch.setattr(o, "_repo_root", lambda: str(tmp_path))
    p = tmp_path / ".claude" / ".fastship-requirements.md"
    p.parent.mkdir(parents=True)
    p.write_text("# 需求定稿\n纯文本没有 json 契约块。" + " 占位内容" * 40)
    orch = {"artifacts": {}}
    _trust(orch, o.REQUIREMENTS_STEP_ID, p)
    orch["artifacts"]["requirements_path"] = str(p)
    ok, msg = o.validate_requirements(orch, {})
    assert ok is False and "JSON" in msg


def test_validate_requirements_rejects_dropped_concern_end_to_end(tmp_path, monkeypatch):
    import orchestrator as o
    monkeypatch.setattr(o, "_repo_root", lambda: str(tmp_path))
    g = valid_gate()
    g["additive_union"] = [u for u in g["additive_union"] if u["id"] != "da-1"]
    p = tmp_path / ".claude" / ".fastship-requirements.md"
    p.parent.mkdir(parents=True)
    p.write_text(requirements_md(g))
    orch = {"artifacts": {}}
    _trust(orch, o.REQUIREMENTS_STEP_ID, p)
    orch["artifacts"]["requirements_path"] = str(p)
    ok, msg = o.validate_requirements(orch, {})
    assert ok is False and "da-1" in msg


# ── Step 1.3r wiring: conditional skip (non-bugfix only) + detection ────────

def _advance(step, request_type):
    import orchestrator as o
    orch = {"current_step": step, "request_type": request_type,
            "completed_steps": [], "skipped_steps": [], "phase": 1}
    return o._advance_state(orch)


def test_step_1_3r_is_registered_after_1_3():
    import orchestrator as o
    ids = [s.id for s in o.STEPS]
    assert "1.3r" in ids
    assert ids.index("1.3r") == ids.index("1.3") + 1  # inserted right after Brief


def test_non_bugfix_runs_1_3r_then_skips_1_3d():
    import orchestrator as o
    orch = _advance("1.3", "feature")
    assert orch["current_step"] == "1.3r"          # 1A runs for features
    orch = o._advance_state(orch)
    assert orch["current_step"] == "1.4"           # then 1.3d (bugfix-only) is skipped
    assert "1.3d" in orch["skipped_steps"]


def test_bugfix_skips_1_3r_and_runs_1_3d():
    orch = _advance("1.3", "bugfix")
    assert orch["current_step"] == "1.3d"          # 1A skipped for bugfix
    assert "1.3r" in orch["skipped_steps"]


def test_detect_completion_1_3r():
    import orchestrator as o
    data = {"tool_input": {"file_path": "/repo/.claude/.fastship-requirements.md"}}
    assert o.detect_completion_post_edit("1.3r", data) == "1.3r"
    # wrong current_step → no detection
    assert o.detect_completion_post_edit("1.3", data) != "1.3r"


def test_requirements_file_allowed_in_phase1():
    import orchestrator as o
    # Phase-1 must let the 1A artifact be written, not BLOCK it as a code file.
    assert o._is_orchestrator_allowed_file("/repo/.claude/.fastship-requirements.md") is True


def test_requirements_filename_owned_by_1_3r():
    import orchestrator as o
    assert o._artifact_owner_step("/repo/.claude/.fastship-requirements.md") == "1.3r"


# ── codex-review findings (GATE FAIL → fixed): close the bypasses ──────────

def test_duplicate_concern_id_fails():
    # [P1] two concerns sharing an id collapse in the set diff, masking a drop.
    g = valid_gate()
    g["roles"][1]["concerns"][0]["id"] = "pr-1"   # collide with 产品's pr-1
    ok, msg = check(g)
    assert ok is False and "重复" in msg


def test_non_abstaining_role_with_empty_concerns_fails():
    # [P2] abstain=false + concerns=[] should fail (no substantive concern → abstain).
    g = valid_gate()
    g["roles"][1]["concerns"] = []                # 数据 claims to participate, says nothing
    ok, msg = check(g)
    assert ok is False and "abstain" in msg


def test_blank_source_entry_fails():
    # [P2] sources=[""] / ["  "] is meaningless provenance.
    g = valid_gate()
    g["additive_union"][0]["sources"] = ["   "]
    ok, msg = check(g)
    assert ok is False and "sources" in msg


def test_additive_union_rewriting_concern_content_fails():
    # [P1 round 2] same id but rewritten point — clerk carries, doesn't edit.
    g = valid_gate()
    g["additive_union"][0]["point"] = "完全不同的内容"   # pr-1 rewritten under same id
    ok, msg = check(g)
    assert ok is False and "改写" in msg


def test_additive_union_misattributed_source_fails():
    # [P1 round 2] pr-1 was raised by 产品; crediting 运营 is misattribution.
    g = valid_gate()
    g["additive_union"][0]["sources"] = ["运营"]
    ok, msg = check(g)
    assert ok is False and "来源" in msg


def test_additive_union_invented_entry_fails():
    # [P1 round 2] a union entry with no originating role concern = clerk invention.
    g = valid_gate()
    g["additive_union"].append({"id": "ghost", "kind": "risk", "point": "凭空", "sources": ["产品"]})
    ok, msg = check(g)
    assert ok is False and "凭空造" in msg


def test_additive_union_extra_source_role_fails():
    # [P2 round 3] crediting an extra role that never raised the concern (id is unique).
    g = valid_gate()
    g["additive_union"][0]["sources"] = ["产品", "运营"]
    ok, msg = check(g)
    assert ok is False and "运营" in msg


def test_missing_tribunal_role_fails():
    # [P2 round 3] a single-role self-review must not satisfy the multi-role gate.
    g = valid_gate()
    g["roles"] = [r for r in g["roles"] if r["role"] != "财务"]
    ok, msg = check(g)
    assert ok is False and "财务" in msg


# ── AC first-class id (1B reference key) ────────────────────────────────────

def test_ac_as_bare_string_fails():
    # ACs must be {id, assertion} objects so 1B can reference each by handle.
    g = valid_gate()
    g["p0"][0]["observable_ac"] = ["改名后昵称在 profile 更新"]
    ok, msg = check(g)
    assert ok is False and "object" in msg


def test_ac_missing_id_fails():
    g = valid_gate()
    g["p0"][0]["observable_ac"] = [{"assertion": "无 id 的 AC"}]
    ok, msg = check(g)
    assert ok is False and "id" in msg


def test_ac_missing_assertion_fails():
    g = valid_gate()
    g["p0"][0]["observable_ac"] = [{"id": "ac-x", "assertion": "  "}]
    ok, msg = check(g)
    assert ok is False and "assertion" in msg


def test_duplicate_ac_id_fails():
    # A duplicate AC id would let 1B's coverage set-comparison hide a gap.
    g = valid_gate()
    g["p0"].append({"id": "p0-2", "source": "用户原话",
                    "observable_ac": [{"id": "ac-1", "assertion": "另一条但 id 撞车"}]})
    ok, msg = check(g)
    assert ok is False and "重复" in msg


# ── P1 enters the same AC discipline + global id namespace ──────────────────

def _with_p1(gate, p1):
    g = gate
    g["p1"] = p1
    return g


def test_valid_gate_with_p1_passes():
    g = _with_p1(valid_gate(), [
        {"id": "p1-1", "source": "brief.md:9",
         "observable_ac": [{"id": "ac-2", "assertion": "改名记入操作日志"}]},
    ])
    ok, msg = check(g)
    assert ok, msg


def test_p1_not_a_list_fails():
    g = valid_gate()
    g["p1"] = {"id": "p1-1"}
    ok, msg = check(g)
    assert ok is False and "p1" in msg


def test_p1_ac_missing_assertion_fails():
    g = _with_p1(valid_gate(), [
        {"id": "p1-1", "source": "brief", "observable_ac": [{"id": "ac-9", "assertion": "  "}]},
    ])
    ok, msg = check(g)
    assert ok is False and "assertion" in msg


def test_p1_ac_id_colliding_with_p0_fails():
    # AC ids are one global namespace across P0/P1 — a P1 AC reusing a P0 AC id
    # would let 1B's coverage diff hide a gap, same dup-id bypass P0 closes.
    g = _with_p1(valid_gate(), [
        {"id": "p1-1", "source": "brief", "observable_ac": [{"id": "ac-1", "assertion": "撞 P0 的 ac-1"}]},
    ])
    ok, msg = check(g)
    assert ok is False and "重复" in msg


def test_p1_without_source_fails():
    g = _with_p1(valid_gate(), [
        {"id": "p1-1", "observable_ac": [{"id": "ac-2", "assertion": "x"}]},
    ])
    ok, msg = check(g)
    assert ok is False and "source" in msg
