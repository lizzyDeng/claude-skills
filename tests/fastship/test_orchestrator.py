import json
import os
import subprocess
import sys
import time
import tempfile
from datetime import datetime
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship'))


def trust_artifact(orch, step_id, path):
    from orchestrator import record_step_artifact
    ok, msg = record_step_artifact(orch, step_id, str(path), source="test")
    assert ok, msg
    return orch["artifacts"]["trusted_artifacts"][step_id]["sha256"]


def write_project_config(root, e2e):
    config_path = root / ".claude" / "fastship.project.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"e2e": e2e}, ensure_ascii=False, indent=2))
    return config_path


def make_trusted_plan(tmp_path, monkeypatch):
    plan_dir = tmp_path / "docs" / "superpowers" / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan = plan_dir / "2026-05-18-feat.md"
    plan.write_text(
        "# Plan\n"
        "> **For agentic workers:** REQUIRED\n"
        "**Goal:** do stuff\n"
        "**Architecture:** stuff\n"
        "**Tech Stack:** python\n"
        "### Task 1\n"
        "- [ ] **Step 1:** write test\n"
    )
    monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
    orch = {"plan_path": str(plan), "artifacts": {}}
    plan_sha = trust_artifact(orch, "1.4", plan)
    return orch, plan, plan_sha


def codex_review_content(plan_sha256="test-plan-sha", **overrides):
    gate = {
        "gate": "PASS",
        "reviewed_plan_sha256": plan_sha256,
        "p0_contract_reviewed": True,
        "ac_e2e_coverage_reviewed": True,
        "weak_case_reviewed": True,
        "evidence_plan_reviewed": True,
        "p0_requirements_missing": [],
        "uncovered_ac": [],
        "unmapped_e2e_scenarios": [],
        "weak_scenarios": [],
        "non_business_assertions": [],
        "missing_evidence": [],
    }
    gate.update(overrides)
    text_gate = gate.get("gate", "PASS")
    return (
        "## Codex Plan Review\n"
        "### Findings\n"
        "- No critical findings\n"
        "### Contract Gate\n"
        "```json\n"
        f"{json.dumps(gate, ensure_ascii=False, indent=2)}\n"
        "```\n"
        f"### GATE: {text_gate}\n"
    )


def code_review_content(**overrides):
    gate = {
        "gate": "PASS",
        "reviewed_against": "design.html",
        "reviewed_files": ["src.py"],
        "design_fidelity_reviewed": True,
        "spec_compliance_reviewed": True,
        "quality_reviewed": True,
        "design_deviations": [],
        "spec_gaps": [],
        "quality_issues": [],
        "unverified_claims": [],
    }
    gate.update(overrides)
    text_gate = gate.get("gate", "PASS")
    return (
        "## Code Review\n"
        "### Per-task verdicts\n"
        "- Task 1: design fidelity OK, spec OK, quality OK\n"
        "### Design Fidelity\n"
        "- Implementation matches the design source pixel treatment\n"
        "### Contract Gate\n"
        "```json\n"
        f"{json.dumps(gate, ensure_ascii=False, indent=2)}\n"
        "```\n"
        f"### GATE: {text_gate}\n"
    )


def make_trusted_code_review(tmp_path, monkeypatch, **overrides):
    monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    design = tmp_path / "design.html"
    design.write_text("<html>design board</html>")
    src = tmp_path / "src.py"
    src.write_text("print('x')")
    fields = {"reviewed_against": str(design), "reviewed_files": [str(src)]}
    fields.update(overrides)
    review = claude / ".fastship-code-review.md"
    review.write_text(code_review_content(**fields))
    orch = {"artifacts": {"code_review_path": str(review)}}
    trust_artifact(orch, "2.5", review)
    return orch, review


# ━━━━━━━━━━━━ Task 1: Core Infrastructure ━━━━━━━━━━━━

class TestStateManagement:
    def test_empty_state_has_required_fields(self):
        from orchestrator import empty_orchestrator_state
        st = empty_orchestrator_state("test req")
        assert st["requirement"] == "test req"
        assert st["current_step"] == "1.0"
        assert st["completed_steps"] == []
        assert st["skipped_steps"] == []
        assert st["phase"] == 1
        assert st["started_at"] is not None

    def test_save_and_load(self, tmp_path):
        from orchestrator import save_orch_state, load_orch_state, empty_orchestrator_state
        f = str(tmp_path / "state.json")
        st = empty_orchestrator_state("req")
        save_orch_state(st, f)
        loaded = load_orch_state(f)
        assert loaded["requirement"] == "req"
        assert loaded["current_step"] == "1.0"

    def test_load_missing_returns_none(self, tmp_path):
        from orchestrator import load_orch_state
        assert load_orch_state(str(tmp_path / "nope.json")) is None

    def test_load_branch_mismatch_keeps_state(self, tmp_path, monkeypatch):
        from orchestrator import save_orch_state, load_orch_state
        import fastship_state
        f = str(tmp_path / "state.json")
        st = {"requirement": "test", "current_step": "1.0", "branch": "feat/old"}
        save_orch_state(st, f)
        monkeypatch.setattr("fastship_state.current_branch", lambda: "main")
        loaded = load_orch_state(f)
        assert loaded is not None
        assert loaded["requirement"] == "test"
        assert fastship_state.branch_mismatch(loaded) is True

    def test_load_branch_match_returns_state(self, tmp_path, monkeypatch):
        from orchestrator import save_orch_state, load_orch_state
        f = str(tmp_path / "state.json")
        st = {"requirement": "test", "current_step": "1.0", "branch": "feat/x"}
        save_orch_state(st, f)
        monkeypatch.setattr("orchestrator._current_branch", lambda: "feat/x")
        loaded = load_orch_state(f)
        assert loaded is not None
        assert loaded["requirement"] == "test"

    def test_installed_tool_prefers_script_repo_over_foreign_cwd(self, tmp_path, monkeypatch):
        import fastship_state

        project = tmp_path / "project"
        other = tmp_path / "other"
        tools = project / ".claude" / "tools"
        tools.mkdir(parents=True)
        other.mkdir()
        subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(other), "init", "-q"], check=True)

        fake_state = tools / "fastship_state.py"
        fake_state.write_text("# test path only\n")
        monkeypatch.setattr(fastship_state, "__file__", str(fake_state))
        monkeypatch.chdir(other)
        # This test exercises the installed-tool vs cwd tier, which sits BELOW the
        # FASTSHIP_REPO_ROOT / CLAUDE_PROJECT_DIR overrides — drop them (the conftest
        # pins FASTSHIP_REPO_ROOT to an empty dir) so the lower tier is reached.
        monkeypatch.delenv("FASTSHIP_REPO_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

        assert fastship_state.repo_root() == str(project.resolve())

    def test_state_paths_are_session_scoped(self, tmp_path, monkeypatch):
        import fastship_state

        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        monkeypatch.setenv("FASTSHIP_SESSION", "Feature A")

        assert fastship_state.orchestrator_state_path().endswith(
            "sessions/feature-a/orchestrator.json"
        )
        assert fastship_state.gate_state_path().endswith(
            "sessions/feature-a/gate.json"
        )

    def test_registry_tracks_multiple_requirement_sessions(self, tmp_path, monkeypatch):
        import fastship_state

        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        a = fastship_state.session_id_from_requirement("fix chat latency")
        b = fastship_state.session_id_from_requirement("fix canvas position")

        fastship_state.set_current_session_id(a, "fix chat latency", {"current_step": "1.2"})
        fastship_state.set_current_session_id(b, "fix canvas position", {"current_step": "2.0"})

        sessions = fastship_state.list_sessions()
        assert set(sessions) == {a, b}
        assert fastship_state.current_session_id() == b


class TestDelegation:
    def test_delegate_to_gate_returns_exit_code(self, tmp_path):
        from orchestrator import delegate_to_gate
        fake_gate = tmp_path / "fake_gate.py"
        fake_gate.write_text("import sys, json; json.load(sys.stdin); print('ok'); sys.exit(0)")
        code, stdout = delegate_to_gate(str(fake_gate), "status", {})
        assert code == 0
        assert "ok" in stdout


# ━━━━━━━━━━━━ Task 2: Validators ━━━━━━━━━━━━

class TestValidatorsPhase1:
    def test_classify_pass(self):
        from orchestrator import validate_classify
        assert validate_classify({}, {"request_classified": True})[0] is True

    def test_classify_fail(self, monkeypatch):
        from orchestrator import validate_classify
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {})
        assert validate_classify({}, {})[0] is False

    def test_recall_pass(self):
        from orchestrator import validate_recall
        assert validate_recall({}, {"knowledge_recall_done": True})[0] is True

    def test_recall_fail(self, monkeypatch):
        from orchestrator import validate_recall
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {})
        assert validate_recall({}, {})[0] is False

    def test_explore_pass(self):
        from orchestrator import validate_explore
        assert validate_explore({"artifacts": {"explore_agents": 3}}, {})[0] is True

    def test_explore_fail_too_few(self):
        from orchestrator import validate_explore
        assert validate_explore({"artifacts": {"explore_agents": 2}}, {})[0] is False

    def test_explore_fail_missing(self):
        from orchestrator import validate_explore
        assert validate_explore({"artifacts": {}}, {})[0] is False

    def test_brief_pass(self, tmp_path):
        from orchestrator import validate_brief
        f = tmp_path / "brief.md"
        f.write_text("## Brief\n### 涉及模块\nx\n### 现有测试\ny\n### 历史变更\nz\n### 历史教训\nw\n" + "p " * 100)
        orch = {"brief_path": str(f), "artifacts": {}}
        trust_artifact(orch, "1.3", f)
        assert validate_brief(orch, {})[0] is True

    def test_brief_fail_missing_section(self, tmp_path):
        from orchestrator import validate_brief
        f = tmp_path / "brief.md"
        f.write_text("### 涉及模块\nx\n" + "p " * 100)
        orch = {"brief_path": str(f), "artifacts": {}}
        trust_artifact(orch, "1.3", f)
        ok, msg = validate_brief(orch, {})
        assert ok is False

    def test_brief_rejects_tampered_artifact(self, tmp_path):
        from orchestrator import validate_brief
        f = tmp_path / "brief.md"
        f.write_text("## Brief\n### 涉及模块\nx\n### 现有测试\ny\n### 历史变更\nz\n### 历史教训\nw\n" + "p " * 100)
        orch = {"brief_path": str(f), "artifacts": {}}
        trust_artifact(orch, "1.3", f)
        f.write_text("## Brief\n### 涉及模块\nchanged\n")
        ok, msg = validate_brief(orch, {})
        assert ok is False
        assert "mismatch" in msg

    def test_brief_fail_no_file(self):
        from orchestrator import validate_brief
        assert validate_brief({"brief_path": "/nonexistent"}, {})[0] is False

    def test_diagnosis_skip_non_bugfix(self):
        from orchestrator import validate_diagnosis
        assert validate_diagnosis({"request_type": "feature"}, {})[0] is True

    def test_diagnosis_fail_bugfix(self, monkeypatch):
        from orchestrator import validate_diagnosis
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {})
        assert validate_diagnosis({"request_type": "bugfix"}, {"bug_diagnosis_done": False})[0] is False

    def test_diagnosis_pass_bugfix(self):
        from orchestrator import validate_diagnosis
        assert validate_diagnosis({"request_type": "bugfix"}, {"bug_diagnosis_done": True})[0] is True

    def test_plan_pass_with_signature(self, tmp_path, monkeypatch):
        from orchestrator import validate_plan
        plan_dir = tmp_path / "docs" / "superpowers" / "plans"
        plan_dir.mkdir(parents=True)
        plan = plan_dir / "2026-05-18-feat.md"
        plan.write_text(
            "# Plan\n"
            "> **For agentic workers:** REQUIRED\n"
            "**Goal:** do stuff\n"
            "**Architecture:** stuff\n"
            "**Tech Stack:** python\n"
            "### Task 1\n"
            "- [ ] **Step 1:** write test\n"
        )
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        orch = {"plan_path": str(plan), "artifacts": {}}
        trust_artifact(orch, "1.4", plan)
        ok, _ = validate_plan(orch, {"plan_ready": True, "plan_file": str(plan)})
        assert ok is True

    def test_plan_fail_no_signature(self, tmp_path, monkeypatch):
        from orchestrator import validate_plan
        plan_dir = tmp_path / "docs" / "superpowers" / "plans"
        plan_dir.mkdir(parents=True)
        plan = plan_dir / "2026-05-18-feat.md"
        plan.write_text("# My hand-written plan\n## Steps\n1. Do stuff\n")
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        orch = {"plan_path": str(plan), "artifacts": {}}
        trust_artifact(orch, "1.4", plan)
        ok, msg = validate_plan(orch, {"plan_ready": True, "plan_file": str(plan)})
        assert ok is False
        assert "签名" in msg or "writing-plans" in msg

    def test_plan_fail_no_file(self, tmp_path, monkeypatch):
        from orchestrator import validate_plan
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, _ = validate_plan({}, {})
        assert ok is False

    def test_grill_pass(self, tmp_path, monkeypatch):
        from orchestrator import validate_grill
        orch, _plan, _plan_sha = make_trusted_plan(tmp_path, monkeypatch)
        grill = tmp_path / ".claude" / ".fastship-grill-result.md"
        grill.parent.mkdir(parents=True)
        grill.write_text(
            "## 拷问记录\n"
            "1. Q: AC 覆盖完整吗 → A: 补了边界 → resolved\n"
            "2. Q: E2E data_source → A: 当前环境 → resolved\n\n"
            "## 修订记录\n"
            "- AC 增加边界条件\n\n"
            "## 结论\n"
            "- 全部 resolved\n"
            + "padding " * 30
        )
        orch["artifacts"]["grill_result_path"] = str(grill)
        trust_artifact(orch, "1.5", grill)
        ok, _ = validate_grill(orch, {})
        assert ok is True

    def test_grill_fail_no_file(self, tmp_path, monkeypatch):
        from orchestrator import validate_grill
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, _ = validate_grill({"artifacts": {}}, {})
        assert ok is False

    def test_grill_fail_missing_section(self, tmp_path, monkeypatch):
        from orchestrator import validate_grill
        orch, _plan, _plan_sha = make_trusted_plan(tmp_path, monkeypatch)
        grill = tmp_path / ".claude" / ".fastship-grill-result.md"
        grill.parent.mkdir(parents=True)
        grill.write_text("## 拷问记录\nstuff\n" + "x " * 200)
        orch["artifacts"]["grill_result_path"] = str(grill)
        trust_artifact(orch, "1.5", grill)
        ok, msg = validate_grill(orch, {})
        assert ok is False
        assert "修订" in msg or "结论" in msg

    def test_grill_fail_too_short(self, tmp_path, monkeypatch):
        from orchestrator import validate_grill
        orch, _plan, _plan_sha = make_trusted_plan(tmp_path, monkeypatch)
        grill = tmp_path / ".claude" / ".fastship-grill-result.md"
        grill.parent.mkdir(parents=True)
        grill.write_text("## 拷问\n## 修订\n## 结论\nok")
        orch["artifacts"]["grill_result_path"] = str(grill)
        trust_artifact(orch, "1.5", grill)
        ok, msg = validate_grill(orch, {})
        assert ok is False
        assert "300B" in msg

    def test_codex_review_rejects_filesystem_fallback(self, tmp_path, monkeypatch):
        from orchestrator import validate_codex_review
        review = tmp_path / ".claude" / ".fastship-codex-review.md"
        review.parent.mkdir(parents=True)
        review.write_text(codex_review_content())
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, msg = validate_codex_review({"artifacts": {}}, {})
        assert ok is False
        assert "fallback" in msg

    def test_codex_review_rejects_text_only_pass(self, tmp_path, monkeypatch):
        from orchestrator import validate_codex_review
        orch, _plan, _plan_sha = make_trusted_plan(tmp_path, monkeypatch)
        review = tmp_path / ".claude" / ".fastship-codex-review.md"
        review.parent.mkdir(parents=True)
        review.write_text(
            "## Codex Plan Review\n### Findings\n- none\n### GATE: PASS\n" + "pad " * 30
        )
        orch["artifacts"]["codex_review_path"] = str(review)
        trust_artifact(orch, "1.5c", review)
        ok, msg = validate_codex_review(orch, {})
        assert ok is False
        assert "JSON gate" in msg

    def test_codex_review_rejects_weak_scenarios(self, tmp_path, monkeypatch):
        from orchestrator import validate_codex_review
        orch, _plan, plan_sha = make_trusted_plan(tmp_path, monkeypatch)
        review = tmp_path / ".claude" / ".fastship-codex-review.md"
        review.parent.mkdir(parents=True)
        review.write_text(codex_review_content(plan_sha, weak_scenarios=["view-offer only checks button visible"]))
        orch["artifacts"]["codex_review_path"] = str(review)
        trust_artifact(orch, "1.5c", review)
        ok, msg = validate_codex_review(orch, {})
        assert ok is False
        assert "weak_scenarios" in msg

    def test_codex_review_rejects_unconfirmed_contract_review(self, tmp_path, monkeypatch):
        from orchestrator import validate_codex_review
        orch, _plan, plan_sha = make_trusted_plan(tmp_path, monkeypatch)
        review = tmp_path / ".claude" / ".fastship-codex-review.md"
        review.parent.mkdir(parents=True)
        review.write_text(codex_review_content(plan_sha, p0_contract_reviewed=False))
        orch["artifacts"]["codex_review_path"] = str(review)
        trust_artifact(orch, "1.5c", review)
        ok, msg = validate_codex_review(orch, {})
        assert ok is False
        assert "p0_contract_reviewed" in msg

    def test_codex_review_rejects_wrong_plan_hash(self, tmp_path, monkeypatch):
        from orchestrator import validate_codex_review
        orch, _plan, _plan_sha = make_trusted_plan(tmp_path, monkeypatch)
        review = tmp_path / ".claude" / ".fastship-codex-review.md"
        review.parent.mkdir(parents=True)
        review.write_text(codex_review_content("wrong-plan-hash"))
        orch["artifacts"]["codex_review_path"] = str(review)
        trust_artifact(orch, "1.5c", review)
        ok, msg = validate_codex_review(orch, {})
        assert ok is False
        assert "plan hash" in msg

    def test_codex_review_passes_with_current_step_artifact(self, tmp_path, monkeypatch):
        from orchestrator import validate_codex_review
        orch, _plan, plan_sha = make_trusted_plan(tmp_path, monkeypatch)
        review = tmp_path / ".claude" / ".fastship-codex-review.md"
        review.parent.mkdir(parents=True)
        review.write_text(codex_review_content(plan_sha))
        orch["artifacts"]["codex_review_path"] = str(review)
        trust_artifact(orch, "1.5c", review)
        ok, _ = validate_codex_review(orch, {})
        assert ok is True

    def test_confirm_pass(self):
        from orchestrator import validate_user_confirm
        assert validate_user_confirm({"artifacts": {"user_confirmed": True}}, {})[0] is True


class TestValidatorsPhase2:
    def test_execute_pass(self):
        from orchestrator import validate_execute
        assert validate_execute({}, {})[0] is True


class TestCodeReviewGate:
    def test_rejects_filesystem_fallback(self, tmp_path, monkeypatch):
        from orchestrator import validate_code_review
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        review = tmp_path / ".claude" / ".fastship-code-review.md"
        review.parent.mkdir(parents=True)
        review.write_text(code_review_content())
        # path not recorded by current step → must refuse filesystem fallback
        ok, msg = validate_code_review({"artifacts": {}}, {})
        assert ok is False
        assert "fallback" in msg

    def test_rejects_text_only_pass(self, tmp_path, monkeypatch):
        from orchestrator import validate_code_review, CODE_REVIEW_FILENAME
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        claude = tmp_path / ".claude"
        claude.mkdir(parents=True)
        review = claude / CODE_REVIEW_FILENAME
        review.write_text("## Code Review\n### GATE: PASS\n" + "x " * 120)  # no JSON gate
        orch = {"artifacts": {"code_review_path": str(review)}}
        trust_artifact(orch, "2.5", review)
        ok, msg = validate_code_review(orch, {})
        assert ok is False
        assert "JSON gate" in msg

    def test_rejects_nonempty_design_deviations(self, tmp_path, monkeypatch):
        from orchestrator import validate_code_review
        orch, _ = make_trusted_code_review(
            tmp_path, monkeypatch, design_deviations=["头像不是径向渐变，背景缺页面径向渐变"])
        ok, msg = validate_code_review(orch, {})
        assert ok is False
        assert "未解决问题" in msg

    def test_rejects_unconfirmed_fidelity(self, tmp_path, monkeypatch):
        from orchestrator import validate_code_review
        orch, _ = make_trusted_code_review(tmp_path, monkeypatch, design_fidelity_reviewed=False)
        ok, msg = validate_code_review(orch, {})
        assert ok is False
        assert "硬审查项" in msg

    def test_rejects_empty_reviewed_against(self, tmp_path, monkeypatch):
        from orchestrator import validate_code_review
        orch, _ = make_trusted_code_review(tmp_path, monkeypatch, reviewed_against="")
        ok, msg = validate_code_review(orch, {})
        assert ok is False
        assert "reviewed_against" in msg

    def test_rejects_nonexistent_reviewed_against(self, tmp_path, monkeypatch):
        from orchestrator import validate_code_review
        orch, _ = make_trusted_code_review(tmp_path, monkeypatch, reviewed_against="/nope/missing-board.html")
        ok, msg = validate_code_review(orch, {})
        assert ok is False
        assert "不存在" in msg

    def test_rejects_nonexistent_reviewed_files(self, tmp_path, monkeypatch):
        from orchestrator import validate_code_review
        orch, _ = make_trusted_code_review(tmp_path, monkeypatch, reviewed_files=["/nope/missing.py"])
        ok, msg = validate_code_review(orch, {})
        assert ok is False
        assert "reviewed_files" in msg

    def test_rejects_fail_verdict(self, tmp_path, monkeypatch):
        from orchestrator import validate_code_review
        orch, _ = make_trusted_code_review(tmp_path, monkeypatch, gate="FAIL")
        ok, msg = validate_code_review(orch, {})
        assert ok is False
        assert "FAIL" in msg

    def test_rejects_tamper_after_record(self, tmp_path, monkeypatch):
        from orchestrator import validate_code_review
        orch, review = make_trusted_code_review(tmp_path, monkeypatch)
        review.write_text(review.read_text() + "\n<!-- tampered after record -->\n")
        ok, msg = validate_code_review(orch, {})
        assert ok is False
        assert ("修改" in msg) or ("mismatch" in msg)

    def test_passes_with_current_step_artifact(self, tmp_path, monkeypatch):
        from orchestrator import validate_code_review
        orch, _ = make_trusted_code_review(tmp_path, monkeypatch)
        ok, msg = validate_code_review(orch, {})
        assert ok is True

    def test_detect_code_review_post_edit(self):
        from orchestrator import detect_completion_post_edit
        data = {"tool_input": {"file_path": "/proj/.claude/.fastship-code-review.md"}}
        assert detect_completion_post_edit("2.5", data) == "2.5"

    def test_no_detect_code_review_wrong_step(self):
        from orchestrator import detect_completion_post_edit
        data = {"tool_input": {"file_path": "/proj/.claude/.fastship-code-review.md"}}
        assert detect_completion_post_edit("1.5c", data) is None

    def test_code_review_flag_registered(self):
        from orchestrator import VALUED_FLAGS
        assert "--code-review" in VALUED_FLAGS


class TestValidatorsPhase3:
    def test_tests_pass(self):
        from orchestrator import validate_tests
        assert validate_tests({}, {"test_passed": True})[0] is True

    def test_tests_fail(self):
        from orchestrator import validate_tests
        assert validate_tests({}, {})[0] is False

    def test_e2e_run_pass(self):
        from orchestrator import validate_e2e_run
        assert validate_e2e_run({}, {"e2e_executed": True})[0] is True

    def test_report_rejects_codex_mode_without_gate(self, tmp_path, monkeypatch):
        from orchestrator import validate_e2e_report
        f = tmp_path / "report.md"
        f.write_text("## Report\n" + "x " * 150)
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {})
        ok, msg = validate_e2e_report({"report_path": str(f)}, {})
        assert ok is False
        assert "fallback" in msg

    def test_report_fail_small_codex_mode(self, tmp_path, monkeypatch):
        from orchestrator import validate_e2e_report
        f = tmp_path / "report.md"
        f.write_text("short")
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {})
        assert validate_e2e_report({"report_path": str(f)}, {})[0] is False

    def test_knowledge_pass(self):
        from orchestrator import validate_knowledge
        hook = {"knowledge_acknowledged": True, "knowledge_skip_reason": "no new lessons"}
        assert validate_knowledge({}, hook)[0] is True

    def test_loop_pass(self, monkeypatch):
        from orchestrator import validate_loop_record
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"test_passed": True, "e2e_executed": True, "e2e_gate_passed": True})
        orch = {"artifacts": {"loop_outcome": "pass"}}
        assert validate_loop_record(orch, {"loop_count": 1})[0] is True

    def test_loop_fail_with_decision(self):
        from orchestrator import validate_loop_record
        orch = {"artifacts": {"loop_outcome": "fail", "loop_decision": "continue"}}
        assert validate_loop_record(orch, {"loop_count": 1})[0] is True

    def test_loop_fail_no_decision(self):
        from orchestrator import validate_loop_record
        orch = {"artifacts": {"loop_outcome": "fail"}}
        assert validate_loop_record(orch, {})[0] is False


class TestValidatorsFallbackDenied:
    def test_plan_fs_fallback_rejected(self, tmp_path, monkeypatch):
        from orchestrator import validate_plan
        plan_dir = tmp_path / "docs" / "superpowers" / "plans"
        plan_dir.mkdir(parents=True)
        (plan_dir / "2026-05-18-feat.md").write_text(
            "# Plan\n> **For agentic workers:** REQUIRED\n"
            "**Goal:** x\n- [ ] **Step 1:** y\n"
        )
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, msg = validate_plan({}, {})
        assert ok is False
        assert "fallback" in msg

    def test_plan_fs_fallback_no_file(self, tmp_path, monkeypatch):
        from orchestrator import validate_plan
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, _ = validate_plan({}, {})
        assert ok is False

    def test_classify_gate_state_fallback(self, tmp_path, monkeypatch):
        from orchestrator import validate_classify
        gate_file = tmp_path / ".claude" / ".ship-verify-state.json"
        gate_file.parent.mkdir(parents=True)
        gate_file.write_text('{"request_classified": true, "request_type": "feature"}')
        monkeypatch.setattr("orchestrator.hook_state_path", lambda: str(gate_file))
        ok, msg = validate_classify({}, {})
        assert ok is True
        assert "feature" in msg

    def test_e2e_run_fs_fallback_rejected(self, tmp_path, monkeypatch):
        from orchestrator import validate_e2e_run
        result_file = tmp_path / "e2e_result.json"
        result_file.write_text(json.dumps({
            "scenarios": [{"rounds": [{"turns": [{"status": 200}] * 12}]}]
        }))
        monkeypatch.setattr("orchestrator.E2E_RESULT_PATH", str(result_file))
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {})
        ok, msg = validate_e2e_run({}, {})
        assert ok is False
        assert "fallback" in msg

    def test_knowledge_fs_fallback_rejected(self, tmp_path, monkeypatch):
        from orchestrator import validate_knowledge
        km = tmp_path / "KNOWLEDGE.md"
        km.write_text("## 2026-05-18 — lesson")
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {})
        orch = {"started_at": "2020-01-01T00:00:00"}
        ok, msg = validate_knowledge(orch, {})
        assert ok is False
        assert "fallback" in msg

    def test_knowledge_fs_fallback_stale(self, tmp_path, monkeypatch):
        from orchestrator import validate_knowledge
        km = tmp_path / "KNOWLEDGE.md"
        km.write_text("## old lesson")
        old_time = time.time() - 7200
        os.utime(str(km), (old_time, old_time))
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        orch = {"started_at": datetime.now().isoformat()}
        ok, _ = validate_knowledge(orch, {})
        assert ok is False


# ━━━━━━━━━━━━ Task 3: Steps + Detection ━━━━━━━━━━━━

class TestSteps:
    def test_step_count(self):
        from orchestrator import STEPS
        assert len(STEPS) == 19

    def test_phase_order(self):
        from orchestrator import STEPS
        phases = [s.phase for s in STEPS]
        for i in range(1, len(phases)):
            assert phases[i] >= phases[i - 1]

    def test_conditional_diagnosis(self):
        from orchestrator import STEPS
        step = next(s for s in STEPS if s.id == "1.3d")
        assert step.conditional == "bugfix"

    def test_all_have_instructions(self):
        from orchestrator import STEPS
        for s in STEPS:
            instruction = s.instruction({}) if callable(s.instruction) else s.instruction
            assert len(instruction) > 30, f"{s.id} instruction too short"

    def test_required_ids_present(self):
        from orchestrator import STEPS
        ids = {s.id for s in STEPS}
        for expected in ["1.0", "1.1", "1.2", "1.3", "1.3d", "1.4", "1.5", "1.5c", "1.6",
                         "2.0", "2.5", "3.0", "3.1", "3.2", "3.3", "3.4", "3.5", "3.6"]:
            assert expected in ids, f"Missing step {expected}"


class TestDetection:
    def test_detect_classify(self):
        from orchestrator import detect_completion_post_bash
        data = {"tool_input": {"command": "python3 .claude/hooks/ship_verify_gate.py classify --type feature"}}
        hook = {"request_classified": True, "request_type": "feature"}
        assert detect_completion_post_bash("1.0", data, hook) == "1.0"

    def test_detect_recall(self):
        from orchestrator import detect_completion_post_bash
        data = {"tool_input": {"command": "python3 .claude/hooks/ship_verify_gate.py knowledge_recall --query test"}}
        hook = {"knowledge_recall_done": True}
        assert detect_completion_post_bash("1.1", data, hook) == "1.1"

    def test_detect_fix_verified(self):
        from orchestrator import detect_completion_post_bash
        data = {"tool_input": {"command": "python3 .claude/hooks/ship_verify_gate.py bug_diagnosis fix_verified"}}
        hook = {"bug_diagnosis_done": True}
        assert detect_completion_post_bash("1.3d", data, hook) == "1.3d"

    def test_detect_test_pass(self):
        from orchestrator import detect_completion_post_bash
        data = {"tool_input": {"command": "pytest tests/ -v"},
                "tool_response": {"stdout": "5 passed in 1.2s"}}
        hook = {"test_passed": True}
        assert detect_completion_post_bash("3.1", data, hook) == "3.1"

    def test_detect_e2e_run(self):
        from orchestrator import detect_completion_post_bash
        data = {"tool_input": {"command": "python3 tests/e2e_runner.py -o /tmp/e2e_result.json"}}
        hook = {"e2e_executed": True}
        assert detect_completion_post_bash("3.2", data, hook) == "3.2"

    def test_detect_loop_record(self):
        from orchestrator import detect_completion_post_bash
        data = {"tool_input": {"command": "python3 .claude/hooks/ship_verify_gate.py loop_record --outcome pass"}}
        hook = {"loop_count": 1, "last_loop_outcome": "pass"}
        assert detect_completion_post_bash("3.5", data, hook) == "3.5"

    def test_no_detect_wrong_step(self):
        from orchestrator import detect_completion_post_bash
        data = {"tool_input": {"command": "pytest tests/"}}
        hook = {"test_passed": True}
        assert detect_completion_post_bash("1.0", data, hook) is None

    def test_detect_brief_post_edit(self):
        from orchestrator import detect_completion_post_edit
        data = {"tool_input": {"file_path": "/proj/.claude/.fastship-brief.md"}}
        assert detect_completion_post_edit("1.3", data) == "1.3"

    def test_detect_plan_post_edit(self):
        from orchestrator import detect_completion_post_edit
        data = {"tool_input": {"file_path": "/proj/docs/superpowers/plans/2026-01-01-feat.md"}}
        assert detect_completion_post_edit("1.4", data) == "1.4"

    def test_detect_grill_post_edit(self):
        from orchestrator import detect_completion_post_edit
        data = {"tool_input": {"file_path": "/proj/.claude/.fastship-grill-result.md"}}
        assert detect_completion_post_edit("1.5", data) == "1.5"

    def test_detect_knowledge_post_edit(self):
        from orchestrator import detect_completion_post_edit
        data = {"tool_input": {"file_path": "/proj/KNOWLEDGE.md"}}
        assert detect_completion_post_edit("3.6", data) == "3.6"


# ━━━━━━━━━━━━ Task 4: Hook Handlers ━━━━━━━━━━━━

class TestHookPreEdit:
    def test_no_session_delegates_to_gate(self):
        from orchestrator import hook_pre_edit_logic
        result = hook_pre_edit_logic(
            data={"tool_input": {"file_path": "src/main.py"}},
            orch_state=None,
            gate_path="/nonexistent",
        )
        assert result == 0

    def test_phase1_blocks_code_edit(self):
        from orchestrator import hook_pre_edit_logic
        orch = {"current_step": "1.2", "phase": 1, "completed_steps": [],
                "skipped_steps": [], "request_type": "feature", "artifacts": {}}
        result = hook_pre_edit_logic(
            data={"tool_input": {"file_path": "src/main.py"}},
            orch_state=orch,
            gate_path="/nonexistent",
        )
        assert result == 1

    def test_phase1_allows_brief_edit(self, tmp_path):
        from orchestrator import hook_pre_edit_logic
        fake_gate = tmp_path / "gate.py"
        fake_gate.write_text("import sys; sys.exit(0)")
        orch = {"current_step": "1.3", "phase": 1, "completed_steps": [],
                "skipped_steps": [], "request_type": "feature", "artifacts": {}}
        result = hook_pre_edit_logic(
            data={"tool_input": {"file_path": ".claude/.fastship-brief.md"}},
            orch_state=orch,
            gate_path=str(fake_gate),
        )
        assert result == 0

    def test_phase1_allows_plan_edit(self, tmp_path):
        from orchestrator import hook_pre_edit_logic
        fake_gate = tmp_path / "gate.py"
        fake_gate.write_text("import sys; sys.exit(0)")
        orch = {"current_step": "1.4", "phase": 1, "completed_steps": [],
                "skipped_steps": [], "request_type": "feature", "artifacts": {}}
        result = hook_pre_edit_logic(
            data={"tool_input": {"file_path": "docs/superpowers/plans/2026-01-01-x.md"}},
            orch_state=orch,
            gate_path=str(fake_gate),
        )
        assert result == 0

    def test_phase2_allows_code_edit(self, tmp_path):
        from orchestrator import hook_pre_edit_logic
        fake_gate = tmp_path / "gate.py"
        fake_gate.write_text("import sys; sys.exit(0)")
        orch = {"current_step": "2.0", "phase": 2, "completed_steps": [],
                "skipped_steps": [], "request_type": "feature", "artifacts": {}}
        result = hook_pre_edit_logic(
            data={"tool_input": {"file_path": "src/main.py"}},
            orch_state=orch,
            gate_path=str(fake_gate),
        )
        assert result == 0


class TestHookPostBash:
    def test_auto_advance_on_classify(self, tmp_path):
        from orchestrator import hook_post_bash_logic, save_orch_state, load_orch_state
        orch_file = str(tmp_path / "orch.json")
        orch = {"current_step": "1.0", "phase": 1, "requirement": "test",
                "completed_steps": [], "skipped_steps": [],
                "request_type": None, "artifacts": {},
                "brief_path": None, "plan_path": None, "report_path": None,
                "loop_count": 0, "started_at": "t", "branch": None}
        save_orch_state(orch, orch_file)
        hook = {"request_classified": True, "request_type": "feature"}

        hook_post_bash_logic(
            data={"tool_input": {"command": "python3 .claude/hooks/ship_verify_gate.py classify --type feature"}},
            orch_path=orch_file,
            hook_state=hook,
        )

        updated = load_orch_state(orch_file)
        assert updated["current_step"] == "1.1"
        assert "1.0" in updated["completed_steps"]


# ━━━━━━━━━━━━ Task 5: CLI ━━━━━━━━━━━━

class TestCLI:
    def test_parse_done_args_valued(self):
        from orchestrator import parse_done_args
        args = parse_done_args(["--agents", "3"])
        assert args["--agents"] == "3"

    def test_parse_done_args_boolean(self):
        from orchestrator import parse_done_args
        args = parse_done_args(["--grill-complete", "--user-confirmed"])
        assert args["--grill-complete"] is True
        assert args["--user-confirmed"] is True

    def test_parse_done_args_mixed(self):
        from orchestrator import parse_done_args
        args = parse_done_args(["--agents", "4", "--grill-complete"])
        assert args["--agents"] == "4"
        assert args["--grill-complete"] is True

    def test_format_status(self):
        from orchestrator import format_status
        orch = {"requirement": "dark mode", "current_step": "1.2", "phase": 1,
                "completed_steps": ["1.0", "1.1"], "skipped_steps": [],
                "loop_count": 0}
        output = format_status(orch)
        assert "dark mode" in output
        assert "✅" in output
        assert "👉" in output

    def test_format_next(self):
        from orchestrator import format_next
        orch = {"current_step": "1.0", "phase": 1}
        output = format_next(orch)
        assert "1.0" in output
        assert "classify" in output

    def test_start_proceeds_without_recent_compact(self, tmp_path, monkeypatch, capsys):
        # Compact is a SOFT advisory, not a hard gate: a stale context must warn but
        # NOT block start (rc != 1). Regression guard for the gate→advisory change.
        from orchestrator import cmd_start
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        monkeypatch.setenv("FASTSHIP_SESSION", "soft compact test")
        monkeypatch.setattr("orchestrator._compact_is_recent", lambda: False)
        monkeypatch.setattr("orchestrator.load_orch_state", lambda *a, **k: None)
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        monkeypatch.setattr("orchestrator.gate_script_path", lambda: str(tmp_path / "absent_gate.py"))
        rc = cmd_start("soft compact test")
        out = capsys.readouterr().out
        assert rc == 0           # not blocked
        assert "SUGGESTION" in out
        assert "Fastship started" in out


# ━━━━━━━━━━━━ Task 6: Integration ━━━━━━━━━━━━

class TestIntegrationFullFlow:
    def test_feature_flow_via_hooks(self, tmp_path, monkeypatch):
        from orchestrator import (
            empty_orchestrator_state, save_orch_state, load_orch_state,
            hook_post_bash_logic, hook_post_edit_logic, hook_pre_edit_logic,
            _advance_state, validate_grill
        )

        orch_file = str(tmp_path / "orch.json")
        st = empty_orchestrator_state("add dark mode")
        st["branch"] = None  # avoid branch check in tests
        save_orch_state(st, orch_file)

        def reload():
            return load_orch_state(orch_file)

        # 1.0: classify (auto via post_bash)
        hook = {"request_classified": True, "request_type": "feature"}
        hook_post_bash_logic(
            data={"tool_input": {"command": "python3 gate classify --type feature"}},
            orch_path=orch_file, hook_state=hook)
        st = reload()
        assert st["current_step"] == "1.1"
        assert st["request_type"] == "feature"

        # Phase 1 blocks code edits
        result = hook_pre_edit_logic(
            data={"tool_input": {"file_path": "src/app.py"}},
            orch_state=st, gate_path="/nonexistent")
        assert result == 1

        # 1.1: recall (auto via post_bash)
        hook["knowledge_recall_done"] = True
        hook_post_bash_logic(
            data={"tool_input": {"command": "python3 gate knowledge_recall --query test"}},
            orch_path=orch_file, hook_state=hook)
        st = reload()
        assert st["current_step"] == "1.2"

        # 1.2: explore (manual done — simulate)
        st["artifacts"]["explore_agents"] = 3
        save_orch_state(st, orch_file)
        st = _advance_state(st)
        save_orch_state(st, orch_file)
        st = reload()
        assert st["current_step"] == "1.3"

        # 1.3: brief (auto via post_edit — filename must contain .fastship-brief.md)
        brief_dir = tmp_path / ".claude"
        brief_dir.mkdir(parents=True, exist_ok=True)
        brief = brief_dir / ".fastship-brief.md"
        brief.write_text(
            "## Brief\n### 涉及模块\nx\n### 现有测试\ny\n"
            "### 历史变更\nz\n### 历史教训\nw\n" + "p " * 100
        )
        st["brief_path"] = str(brief)
        save_orch_state(st, orch_file)
        hook_post_edit_logic(
            data={"tool_input": {"file_path": str(brief)}},
            orch_path=orch_file)
        st = reload()
        assert st["current_step"] == "1.3r"   # 1A requirements tribunal runs for features

        # 1.3r: requirements-lock (auto via post_edit)
        req_gate = {
            "roles": [
                {"role": "产品", "abstain": False, "concerns": [
                    {"id": "c1", "kind": "ac", "point": "dark mode toggle", "evidence_ref": "用户原话"}]},
                {"role": "运营", "abstain": True, "concerns": []},
                {"role": "数据", "abstain": True, "concerns": []},
                {"role": "财务", "abstain": True, "concerns": []},
            ],
            "additive_union": [{"id": "c1", "kind": "ac", "point": "dark mode toggle", "sources": ["产品"]}],
            "exclusive_forks": [],
            "p0": [{"id": "p0-1", "source": "用户原话", "observable_ac": ["切换后主题变暗"]}],
        }
        req = brief_dir / ".fastship-requirements.md"
        req.write_text("# 需求定稿\n## 契约\n```json\n" + json.dumps(req_gate, ensure_ascii=False)
                       + "\n```\n" + "占位 " * 20)
        hook_post_edit_logic(
            data={"tool_input": {"file_path": str(req)}},
            orch_path=orch_file)
        st = reload()
        assert st["current_step"] == "1.4"
        assert "1.3d" in st["skipped_steps"]

        # 1.4: plan (auto via post_edit) — needs signature
        plan_dir = tmp_path / "docs" / "superpowers" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "2026-05-18-dark.md"
        plan_file.write_text(
            "# Plan\n> **For agentic workers:** REQUIRED\n"
            "**Goal:** dark mode\n- [ ] **Step 1:** test\n"
        )
        hook["plan_ready"] = True
        hook["plan_file"] = str(plan_file)
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        hook_post_edit_logic(
            data={"tool_input": {"file_path": str(plan_file)}},
            orch_path=orch_file)
        st = reload()
        assert st["current_step"] == "1.5"

        # 1.5: grill (auto via post_edit when grill result file written)
        grill_result = tmp_path / ".claude" / ".fastship-grill-result.md"
        grill_result.parent.mkdir(parents=True, exist_ok=True)
        grill_result.write_text(
            "## 拷问记录\n1. Q: AC? → A: ok → resolved\n"
            "## 修订记录\n- none\n"
            "## 结论\n- resolved\n" + "x " * 150
        )
        hook_post_edit_logic(
            data={"tool_input": {"file_path": str(grill_result)}},
            orch_path=orch_file)
        st = reload()
        assert st["current_step"] == "1.5c"

        # 1.5c: codex review (auto via post_edit when review file written)
        plan_sha = st["artifacts"]["trusted_artifacts"]["1.4"]["sha256"]
        codex_review = tmp_path / ".claude" / ".fastship-codex-review.md"
        codex_review.write_text(codex_review_content(plan_sha))
        hook_post_edit_logic(
            data={"tool_input": {"file_path": str(codex_review)}},
            orch_path=orch_file)
        st = reload()
        assert st["current_step"] == "1.6"

        # 1.6: confirm (manual done)
        st["artifacts"]["user_confirmed"] = True
        save_orch_state(st, orch_file)
        st = _advance_state(st)
        save_orch_state(st, orch_file)
        st = reload()
        assert st["current_step"] == "2.0"
        assert st["phase"] == 2

        # Phase 2 allows code edits
        fake_gate = tmp_path / "gate.py"
        fake_gate.write_text("import sys; sys.exit(0)")
        result = hook_pre_edit_logic(
            data={"tool_input": {"file_path": "src/app.py"}},
            orch_state=st, gate_path=str(fake_gate))
        assert result == 0

        # 2.0 → 2.5 → 3.0 → 3.1: manual done (validators bypassed via _advance_state)
        st = _advance_state(st)  # → 2.5
        st = _advance_state(st)  # → 3.0
        st = _advance_state(st)  # → 3.1
        save_orch_state(st, orch_file)
        st = reload()
        assert st["current_step"] == "3.1"

        # 3.1: tests (auto)
        hook["test_passed"] = True
        hook_post_bash_logic(
            data={"tool_input": {"command": "pytest tests/ -v"}},
            orch_path=orch_file, hook_state=hook)
        st = reload()
        assert st["current_step"] == "3.2"

        # 3.2: e2e run (auto)
        hook["e2e_executed"] = True
        hook_post_bash_logic(
            data={"tool_input": {"command": "python3 tests/e2e_runner.py -o /tmp/e2e.json"}},
            orch_path=orch_file, hook_state=hook)
        st = reload()
        assert st["current_step"] == "3.3"

        # 3.3: report (auto via post_edit)
        # Set up e2e_result.json + gate hash so validate_e2e_report passes
        import hashlib
        e2e_data = {"scenarios": [{"rounds": [{"turns": [{"status": 200}] * 12}]}]}
        e2e_bytes = json.dumps(e2e_data, ensure_ascii=False).encode("utf-8")
        e2e_file = tmp_path / "e2e_result.json"
        e2e_file.write_bytes(e2e_bytes)
        e2e_hash = hashlib.sha256(e2e_bytes).hexdigest()
        monkeypatch.setattr("orchestrator.E2E_RESULT_PATH", str(e2e_file))
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"e2e_executed": True, "e2e_result_hash": e2e_hash})
        report = tmp_path / "report.md"
        report.write_text(f"## Report\n\ne2e_result_hash: {e2e_hash}\n" + "x " * 150)
        st["report_path"] = str(report)
        save_orch_state(st, orch_file)
        hook_post_edit_logic(
            data={"tool_input": {"file_path": str(report)}},
            orch_path=orch_file)
        st = reload()
        assert st["current_step"] == "3.4"

        # 3.4: gate (auto) — needs exit code 0 + validate_e2e_gate pass
        gate_script = tmp_path / "tests" / "e2e_gate.py"
        gate_script.parent.mkdir(parents=True, exist_ok=True)
        gate_script.write_text("import sys; print('GATE PASSED'); sys.exit(0)")
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {
                                "test_passed": True,
                                "e2e_executed": True,
                                "e2e_result_hash": e2e_hash,
                                "e2e_gate_passed": True,
                            })
        hook_post_bash_logic(
            data={"tool_input": {"command": "python3 tests/e2e_gate.py --result /tmp/e2e.json"},
                  "tool_response": {"exitCode": 0, "stdout": "GATE PASSED"}},
            orch_path=orch_file, hook_state=hook)
        st = reload()
        assert st["current_step"] == "3.5"

        # 3.5: loop record pass (auto)
        hook["loop_count"] = 1
        hook["last_loop_outcome"] = "pass"
        hook_post_bash_logic(
            data={"tool_input": {"command": "python3 gate loop_record --outcome pass"}},
            orch_path=orch_file, hook_state=hook)
        st = reload()
        assert st["current_step"] == "3.6"

        # 3.6: knowledge (auto) — write KNOWLEDGE.md in tmp_path so mtime check passes
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {
                                "test_passed": True,
                                "e2e_executed": True,
                                "e2e_result_hash": e2e_hash,
                                "e2e_gate_passed": True,
                                "knowledge_acknowledged": True,
                                "knowledge_file": str(tmp_path / "KNOWLEDGE.md"),
                            })
        km = tmp_path / "KNOWLEDGE.md"
        km.write_text("## 2026-05-18 — lesson learned")
        hook_post_edit_logic(
            data={"tool_input": {"file_path": str(km)}},
            orch_path=orch_file)
        st = reload()
        assert st["current_step"] == "done"

    def test_loop_fail_pauses_for_decision(self, tmp_path):
        from orchestrator import save_orch_state, load_orch_state, hook_post_bash_logic
        orch_file = str(tmp_path / "orch.json")
        st = {
            "requirement": "test", "current_step": "3.5", "phase": 3,
            "completed_steps": ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.5c", "1.6",
                                "2.0", "3.0", "3.1", "3.2", "3.3", "3.4"],
            "skipped_steps": ["1.3d"], "request_type": "feature",
            "loop_count": 0, "artifacts": {}, "branch": None,
            "brief_path": None, "plan_path": None, "report_path": None,
            "started_at": "t",
        }
        save_orch_state(st, orch_file)

        hook = {"loop_count": 1, "last_loop_outcome": "fail"}
        hook_post_bash_logic(
            data={"tool_input": {"command": "python3 gate loop_record --outcome fail --reflection p"}},
            orch_path=orch_file, hook_state=hook)

        st = load_orch_state(orch_file)
        assert st["current_step"] == "3.5"
        assert st["artifacts"]["loop_outcome"] == "fail"

    def test_loop_fail_continue_via_done(self):
        from orchestrator import _handle_loop_decision
        st = {
            "current_step": "3.5", "phase": 3, "loop_count": 1,
            "completed_steps": ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.5c", "1.6",
                                "2.0", "2.5", "3.0", "3.1", "3.2", "3.3", "3.4"],
            "skipped_steps": ["1.3d"], "request_type": "feature",
            "artifacts": {
                "loop_outcome": "fail", "loop_decision": "continue",
                "code_review_path": "/proj/.claude/.fastship-code-review.md",
                "trusted_artifacts": {"2.5": {"step_id": "2.5", "sha256": "x"}},
            },
        }
        _handle_loop_decision(st)
        # Loop continue must re-review: re-enter at 2.5 (phase 2), clearing 2.5 + 3.x
        # and dropping the stale code-review artifact so a fresh review is forced.
        assert st["current_step"] == "2.5"
        assert st["phase"] == 2
        assert "2.5" not in st["completed_steps"]
        assert "3.1" not in st["completed_steps"]
        assert "1.0" in st["completed_steps"]
        assert "code_review_path" not in st["artifacts"]
        assert "2.5" not in st["artifacts"].get("trusted_artifacts", {})

    def test_loop_fail_escalate_via_done(self):
        from orchestrator import _handle_loop_decision
        st = {
            "current_step": "3.5", "phase": 3, "loop_count": 1,
            "completed_steps": ["1.0", "1.1", "2.0", "3.1", "3.2"],
            "skipped_steps": [], "request_type": "feature",
            "artifacts": {"loop_outcome": "fail", "loop_decision": "escalate"},
        }
        _handle_loop_decision(st)
        assert st["current_step"] == "1.0"
        assert st["completed_steps"] == []

    def test_loop_fail_stop_via_done(self):
        from orchestrator import _handle_loop_decision
        st = {
            "current_step": "3.5", "phase": 3, "loop_count": 2,
            "completed_steps": ["1.0"], "skipped_steps": [],
            "artifacts": {"loop_outcome": "fail", "loop_decision": "stop"},
        }
        _handle_loop_decision(st)
        assert st["current_step"] == "stopped"


class TestGateAExtended:
    """Gate A must protect gate.json and orchestrator.json, not just legacy paths."""

    def test_blocks_edit_gate_json(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import ship_verify_gate
        assert ship_verify_gate.is_fastship_state_file(".git/fastship/gate.json") is True

    def test_blocks_edit_orchestrator_json(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import ship_verify_gate
        assert ship_verify_gate.is_fastship_state_file(".git/fastship/orchestrator.json") is True

    def test_allows_non_state_files(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import ship_verify_gate
        assert ship_verify_gate.is_fastship_state_file("src/main.py") is False
        assert ship_verify_gate.is_fastship_state_file("gate.json") is False

    def test_blocks_bash_write_to_gate(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import ship_verify_gate
        assert ship_verify_gate.is_state_file_write_cmd('echo \'{"e2e_executed":true}\' > .git/fastship/gate.json') is True
        assert ship_verify_gate.is_state_file_write_cmd('python3 -c "..." > .git/fastship/gate.json') is True
        assert ship_verify_gate.is_state_file_write_cmd('cat /tmp/fake.json > .git/fastship/gate.json') is True

    def test_allows_gate_script_without_redirect(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import ship_verify_gate
        assert ship_verify_gate.is_state_file_write_cmd('python3 .claude/hooks/ship_verify_gate.py classify --type feature') is False

    def test_blocks_gate_script_with_redirect(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import ship_verify_gate
        assert ship_verify_gate.is_state_file_write_cmd('python3 .claude/hooks/ship_verify_gate.py status > .git/fastship/gate.json') is True

    def test_allows_normal_bash(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import ship_verify_gate
        assert ship_verify_gate.is_state_file_write_cmd('cargo test') is False
        assert ship_verify_gate.is_state_file_write_cmd('cat .git/fastship/gate.json') is False


class TestStrictRunnerProvenance:
    """is_strict_e2e_runner must distinguish real runners from fake commands."""

    def test_matches_python_e2e_runner(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import importlib, ship_verify_gate
        importlib.reload(ship_verify_gate)
        assert ship_verify_gate.is_strict_e2e_runner('python3 tests/e2e_runner.py -o /tmp/e2e.json') is True

    def test_rejects_echo_e2e(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import importlib, ship_verify_gate
        importlib.reload(ship_verify_gate)
        assert ship_verify_gate.is_strict_e2e_runner('echo e2e test passed') is False

    def test_rejects_curl(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import importlib, ship_verify_gate
        importlib.reload(ship_verify_gate)
        assert ship_verify_gate.is_strict_e2e_runner('curl http://localhost:3100/api/chat') is False

    def test_matches_playwright(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import importlib, ship_verify_gate
        importlib.reload(ship_verify_gate)
        assert ship_verify_gate.is_strict_e2e_runner('playwright test tests/e2e/') is True

    def test_matches_npm_run_e2e(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import importlib, ship_verify_gate
        importlib.reload(ship_verify_gate)
        assert ship_verify_gate.is_strict_e2e_runner('npm run test:e2e') is True


class TestE2ERunHardened:
    """validate_e2e_run should only trust gate.json when hooks are active."""

    def test_rejects_file_when_gate_exists(self, tmp_path, monkeypatch):
        from orchestrator import validate_e2e_run
        result_file = tmp_path / "e2e_result.json"
        result_file.write_text('{"scenarios": []}')
        monkeypatch.setattr("orchestrator.E2E_RESULT_PATH", str(result_file))
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {"e2e_executed": False, "branch": "test"})
        ok, msg = validate_e2e_run({}, {})
        assert ok is False

    def test_accepts_gate_executed(self, monkeypatch):
        from orchestrator import validate_e2e_run
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {"e2e_executed": True})
        ok, _ = validate_e2e_run({}, {})
        assert ok is True

    def test_codex_fallback_rejected_even_with_low_quality_file(self, tmp_path, monkeypatch):
        from orchestrator import validate_e2e_run
        result_file = tmp_path / "e2e_result.json"
        result_file.write_text(json.dumps({
            "scenarios": [{"rounds": [{"turns": [{"status": 200}] * 5}]}]
        }))
        monkeypatch.setattr("orchestrator.E2E_RESULT_PATH", str(result_file))
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {})
        ok, msg = validate_e2e_run({}, {})
        assert ok is False
        assert "fallback" in msg

    def test_codex_fallback_rejected_even_with_quality_file(self, tmp_path, monkeypatch):
        from orchestrator import validate_e2e_run
        result_file = tmp_path / "e2e_result.json"
        result_file.write_text(json.dumps({
            "scenarios": [{"rounds": [{"turns": [{"status": 200}] * 12}]}]
        }))
        monkeypatch.setattr("orchestrator.E2E_RESULT_PATH", str(result_file))
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {})
        ok, msg = validate_e2e_run({}, {})
        assert ok is False
        assert "fallback" in msg


class TestE2EReportHardened:
    """validate_e2e_report must verify data integrity via gate.json hash."""

    def test_rejects_without_hash(self, tmp_path, monkeypatch):
        from orchestrator import validate_e2e_report
        report = tmp_path / "report.md"
        report.write_text("## Report\n" + "x " * 150)
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"e2e_executed": True})
        ok, _ = validate_e2e_report({"report_path": str(report)}, {})
        assert ok is False

    def test_rejects_hash_mismatch(self, tmp_path, monkeypatch):
        import hashlib
        from orchestrator import validate_e2e_report
        result_file = tmp_path / "e2e_result.json"
        original = json.dumps({"scenarios": [{"rounds": [{"turns": [{"status": 200}] * 12}]}]})
        recorded_hash = hashlib.sha256(original.encode()).hexdigest()
        result_file.write_text('{"scenarios": [{"rounds": [{"turns": []}]}]}')
        report = tmp_path / "report.md"
        report.write_text("## Report\n" + "x " * 150)
        monkeypatch.setattr("orchestrator.E2E_RESULT_PATH", str(result_file))
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"e2e_executed": True, "e2e_result_hash": recorded_hash})
        ok, msg = validate_e2e_report({"report_path": str(report)}, {})
        assert ok is False
        assert "mismatch" in msg.lower() or "hash" in msg.lower()

    def test_passes_with_valid_hash(self, tmp_path, monkeypatch):
        import hashlib
        from orchestrator import validate_e2e_report
        result_data = {"scenarios": [{"rounds": [{"turns": [{"status": 200}] * 12}]}]}
        result_bytes = json.dumps(result_data, ensure_ascii=False).encode("utf-8")
        result_file = tmp_path / "e2e_result.json"
        result_file.write_bytes(result_bytes)
        recorded_hash = hashlib.sha256(result_bytes).hexdigest()
        report = tmp_path / "report.md"
        report.write_text(f"## Report\n\ne2e_result_hash: {recorded_hash}\n" + "x " * 150)
        orch = {"report_path": str(report), "artifacts": {}}
        trust_artifact(orch, "3.3", report)
        monkeypatch.setattr("orchestrator.E2E_RESULT_PATH", str(result_file))
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"e2e_executed": True, "e2e_result_hash": recorded_hash})
        ok, _ = validate_e2e_report(orch, {})
        assert ok is True

    def test_rejects_report_missing_result_hash_reference(self, tmp_path, monkeypatch):
        import hashlib
        from orchestrator import validate_e2e_report
        result_data = {"scenarios": [{"rounds": [{"turns": [{"status": 200}] * 12}]}]}
        result_bytes = json.dumps(result_data, ensure_ascii=False).encode("utf-8")
        result_file = tmp_path / "e2e_result.json"
        result_file.write_bytes(result_bytes)
        recorded_hash = hashlib.sha256(result_bytes).hexdigest()
        report = tmp_path / "report.md"
        report.write_text("## Report\n\nAll good, but no raw result hash.\n" + "x " * 150)
        orch = {"report_path": str(report), "artifacts": {}}
        trust_artifact(orch, "3.3", report)
        monkeypatch.setattr("orchestrator.E2E_RESULT_PATH", str(result_file))
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"e2e_executed": True, "e2e_result_hash": recorded_hash})
        ok, msg = validate_e2e_report(orch, {})
        assert ok is False
        assert "e2e_result_hash" in msg

    def test_codex_fallback_is_rejected(self, tmp_path, monkeypatch):
        from orchestrator import validate_e2e_report
        report = tmp_path / "report.md"
        report.write_text("## Report\n" + "x " * 150)
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {})
        ok, msg = validate_e2e_report({"report_path": str(report)}, {})
        assert ok is False
        assert "fallback" in msg


class TestE2EGateHardened:
    """validate_e2e_gate must run gate script as subprocess, not auto-pass."""

    def test_rejects_when_gate_script_missing(self, monkeypatch):
        from orchestrator import validate_e2e_gate
        monkeypatch.setattr("orchestrator._repo_root", lambda: "/nonexistent")
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"e2e_executed": True})
        ok, _ = validate_e2e_gate({}, {})
        assert ok is False

    def test_passes_when_gate_exits_zero(self, tmp_path, monkeypatch):
        from orchestrator import validate_e2e_gate
        gate_script = tmp_path / "tests" / "e2e_gate.py"
        gate_script.parent.mkdir(parents=True)
        gate_script.write_text("import sys; print('GATE PASSED'); sys.exit(0)")
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"e2e_executed": True})
        ok, msg = validate_e2e_gate({}, {})
        assert ok is True
        assert "passed" in msg.lower()

    def test_rejects_when_gate_exits_nonzero(self, tmp_path, monkeypatch):
        from orchestrator import validate_e2e_gate
        gate_script = tmp_path / "tests" / "e2e_gate.py"
        gate_script.parent.mkdir(parents=True)
        gate_script.write_text("import sys; print('BLOCKED: not enough turns'); sys.exit(1)")
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"e2e_executed": True})
        ok, msg = validate_e2e_gate({}, {})
        assert ok is False

    def test_codex_fallback_is_rejected(self, monkeypatch):
        from orchestrator import validate_e2e_gate
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {})
        ok, msg = validate_e2e_gate({}, {})
        assert ok is False
        assert "fallback" in msg


class TestProjectE2EConfig:
    """Project config is the single source for local E2E setup and gate args."""

    def test_missing_project_config_returns_empty(self, tmp_path, monkeypatch):
        import fastship_state
        monkeypatch.setattr("fastship_state.repo_root", lambda: str(tmp_path))
        assert fastship_state.load_project_config() == {}

    def test_format_next_e2e_runner_uses_project_config(self, tmp_path, monkeypatch):
        from orchestrator import format_next

        result_path = tmp_path / "e2e_result.json"
        write_project_config(tmp_path, {
            "setup_commands": ["./dev_local.sh"],
            "runner_command": f"python3 tests/e2e_runner.py --base-url http://localhost:3100 --health /health -o {result_path}",
            "gate_command": f"python3 tests/e2e_gate.py --result {result_path} --min-turns 12",
            "result_path": str(result_path),
            "min_turns": 12,
            "notes": ["Use dev_local.sh for local services."],
        })
        monkeypatch.setattr("fastship_state.repo_root", lambda: str(tmp_path))

        output = format_next({"current_step": "3.2", "phase": 3})

        assert "./dev_local.sh" in output
        assert "--base-url http://localhost:3100" in output
        assert f"原始结果必须写入 {result_path}" in output
        assert "最少 12 轮" in output
        assert "Use dev_local.sh" in output

    def test_format_next_e2e_gate_uses_project_config(self, tmp_path, monkeypatch):
        from orchestrator import format_next

        result_path = tmp_path / "custom-result.json"
        write_project_config(tmp_path, {
            "gate_command": f"python3 tests/e2e_gate.py --result {result_path} --min-turns 17",
            "result_path": str(result_path),
            "min_turns": 17,
        })
        monkeypatch.setattr("fastship_state.repo_root", lambda: str(tmp_path))

        output = format_next({"current_step": "3.4", "phase": 3})

        assert f"--result {result_path}" in output
        assert "--min-turns 17" in output

    def test_validate_e2e_gate_passes_configured_result_and_min_turns(self, tmp_path, monkeypatch):
        from orchestrator import validate_e2e_gate

        result_path = tmp_path / "custom-result.json"
        argv_path = tmp_path / "argv.json"
        write_project_config(tmp_path, {
            "result_path": str(result_path),
            "min_turns": 17,
        })
        gate_script = tmp_path / "tests" / "e2e_gate.py"
        gate_script.parent.mkdir(parents=True)
        gate_script.write_text(
            "import json, sys\n"
            f"open({str(argv_path)!r}, 'w').write(json.dumps(sys.argv))\n"
            "sys.exit(0)\n"
        )
        monkeypatch.setattr("fastship_state.repo_root", lambda: str(tmp_path))
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"e2e_executed": True})

        ok, msg = validate_e2e_gate({}, {})

        assert ok is True, msg
        argv = json.loads(argv_path.read_text())
        assert argv[argv.index("--result") + 1] == str(result_path)
        assert argv[argv.index("--min-turns") + 1] == "17"

    def test_hook_gate_matches_configured_runner_and_result_path(self, tmp_path, monkeypatch):
        hooks_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks')
        sys.path.insert(0, hooks_dir)
        import importlib, ship_verify_gate
        importlib.reload(ship_verify_gate)

        result_path = tmp_path / "custom-result.json"
        result_path.write_text(json.dumps({"status": "pass", "scenarios": []}))
        write_project_config(tmp_path, {
            "runner_command": "./scripts/run-local-e2e",
            "result_path": str(result_path),
        })
        monkeypatch.setattr("fastship_state.repo_root", lambda: str(tmp_path))

        assert ship_verify_gate.is_e2e_cmd("./scripts/run-local-e2e") is True
        assert ship_verify_gate.is_strict_e2e_runner("./scripts/run-local-e2e") is True
        ok, reason = ship_verify_gate.e2e_succeeded({"tool_response": {"stdout": ""}})
        assert ok is True, reason


class TestDetectionE2EGateHardened:
    """detect_completion_post_bash for 3.4 must check exit code."""

    def test_advances_on_exit_zero(self):
        from orchestrator import detect_completion_post_bash
        data = {
            "tool_input": {"command": "python3 tests/e2e_gate.py --result /tmp/e2e.json"},
            "tool_response": {"exitCode": 0, "stdout": "GATE PASSED"},
        }
        assert detect_completion_post_bash("3.4", data, {}) == "3.4"

    def test_blocks_on_exit_nonzero(self):
        from orchestrator import detect_completion_post_bash
        data = {
            "tool_input": {"command": "python3 tests/e2e_gate.py --result /tmp/e2e.json"},
            "tool_response": {"exitCode": 1, "stdout": "BLOCKED"},
        }
        assert detect_completion_post_bash("3.4", data, {}) is None

    def test_blocks_on_unknown_exit_no_gate_passed(self):
        from orchestrator import detect_completion_post_bash
        data = {
            "tool_input": {"command": "python3 tests/e2e_gate.py --result /tmp/e2e.json"},
            "tool_response": {"stdout": "something else"},
        }
        assert detect_completion_post_bash("3.4", data, {}) is None


class TestLoopRecordHardened:
    """validate_loop_record must cross-check gate.json when outcome=pass."""

    def test_pass_rejected_without_gate_flags(self, monkeypatch):
        from orchestrator import validate_loop_record
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"test_passed": False, "e2e_executed": False, "e2e_gate_passed": False})
        orch = {"artifacts": {"loop_outcome": "pass"}}
        ok, _ = validate_loop_record(orch, {})
        assert ok is False

    def test_pass_rejected_without_gate_passed(self, monkeypatch):
        from orchestrator import validate_loop_record
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"test_passed": True, "e2e_executed": True, "e2e_gate_passed": False})
        orch = {"artifacts": {"loop_outcome": "pass"}}
        ok, msg = validate_loop_record(orch, {})
        assert ok is False
        assert "gate_passed" in msg

    def test_pass_accepted_with_all_gate_flags(self, monkeypatch):
        from orchestrator import validate_loop_record
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"test_passed": True, "e2e_executed": True, "e2e_gate_passed": True})
        orch = {"artifacts": {"loop_outcome": "pass"}}
        ok, _ = validate_loop_record(orch, {})
        assert ok is True

    def test_pass_rejected_codex_mode_without_gate(self, monkeypatch):
        from orchestrator import validate_loop_record
        monkeypatch.setattr("orchestrator._read_gate_state_file", lambda: {})
        orch = {"artifacts": {"loop_outcome": "pass"}}
        ok, msg = validate_loop_record(orch, {})
        assert ok is False
        assert "fallback" in msg


class TestFabricationBlocked:
    """End-to-end test: fabrication paths must be blocked after hardening."""

    def test_fake_e2e_result_blocked(self, tmp_path, monkeypatch):
        """Claude creates /tmp/e2e_result.json directly → should not pass 3.2."""
        from orchestrator import validate_e2e_run

        fake_result = tmp_path / "e2e_result.json"
        fake_result.write_text(json.dumps({
            "scenarios": [{"rounds": [{"turns": [{"status": 200}] * 15}]}]
        }))
        monkeypatch.setattr("orchestrator.E2E_RESULT_PATH", str(fake_result))
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"e2e_executed": False, "branch": "test"})

        ok, msg = validate_e2e_run({}, {})
        assert ok is False, f"Fabricated e2e_result.json should be rejected, got: {msg}"

    def test_fake_report_blocked(self, tmp_path, monkeypatch):
        """Claude writes a >=200B report but gate.json has no hash → should not pass 3.3."""
        from orchestrator import validate_e2e_report

        report = tmp_path / "e2e-quality-report.md"
        report.write_text(
            "## E2E 质量检测报告\n\n"
            "本 feature 涉及 LLM 意图识别，无法自动化 E2E。\n"
            "手动验证结果如下：\n"
            "1. 测试对话场景 A — 通过\n"
            "2. 测试对话场景 B — 通过\n"
            "3. 边界测试 — 通过\n\n"
            "总体通过率: 100%\n"
        )
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"e2e_executed": True})

        ok, msg = validate_e2e_report({"report_path": str(report)}, {})
        assert ok is False, f"Fabricated report should be rejected, got: {msg}"

    def test_self_grading_pass_blocked(self, monkeypatch):
        """Claude calls done --outcome pass but gate shows tests failed → blocked."""
        from orchestrator import validate_loop_record

        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"test_passed": False, "e2e_executed": True, "e2e_gate_passed": True})

        orch = {"artifacts": {"loop_outcome": "pass"}}
        ok, msg = validate_loop_record(orch, {})
        assert ok is False, f"Self-grading should be rejected, got: {msg}"

    def test_hash_tampered_between_steps(self, tmp_path, monkeypatch):
        """e2e_result.json modified between 3.3 and 3.4 → gate recheck catches it."""
        import hashlib
        from orchestrator import validate_e2e_gate

        original = json.dumps({"scenarios": [{"rounds": [{"turns": [{"status": 200}] * 12}]}]})
        original_hash = hashlib.sha256(original.encode()).hexdigest()

        result_file = tmp_path / "e2e_result.json"
        result_file.write_text('{"scenarios": [{"rounds": [{"turns": []}]}]}')

        monkeypatch.setattr("orchestrator.E2E_RESULT_PATH", str(result_file))
        monkeypatch.setattr("orchestrator._read_gate_state_file",
                            lambda: {"e2e_executed": True, "e2e_result_hash": original_hash})
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))

        ok, msg = validate_e2e_gate({}, {})
        assert ok is False
        assert "mismatch" in msg.lower() or "hash" in msg.lower()

    def test_fake_e2e_cmd_no_provenance(self):
        """A command containing 'e2e' but not a real runner → no hash recorded."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import importlib, ship_verify_gate
        importlib.reload(ship_verify_gate)
        assert ship_verify_gate.is_strict_e2e_runner('echo e2e test passed') is False
        assert ship_verify_gate.is_strict_e2e_runner('curl http://localhost:3100/api/chat') is False
        assert ship_verify_gate.is_strict_e2e_runner('python3 tests/e2e_runner.py -o /tmp/e2e.json') is True

    def test_gate_state_file_edit_blocked(self):
        """Edit/Write to gate.json must be blocked by Gate A."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import importlib, ship_verify_gate
        importlib.reload(ship_verify_gate)
        assert ship_verify_gate.is_fastship_state_file(".git/fastship/gate.json") is True

    def test_gate_state_bash_write_blocked(self):
        """Bash write to gate.json must be blocked."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks'))
        import importlib, ship_verify_gate
        importlib.reload(ship_verify_gate)
        assert ship_verify_gate.is_state_file_write_cmd(
            'echo \'{"e2e_executed":true}\' > .git/fastship/gate.json') is True

    def test_e2e_gate_exit_nonzero_blocked(self):
        """e2e_gate command with exit code 1 must not advance step 3.4."""
        from orchestrator import detect_completion_post_bash
        data = {
            "tool_input": {"command": "python3 tests/e2e_gate.py --result /tmp/e2e.json"},
            "tool_response": {"exitCode": 1, "stdout": "BLOCKED: not enough turns"},
        }
        assert detect_completion_post_bash("3.4", data, {}) is None


class TestStepArtifactGuard:
    """Prevent out-of-order step artifact writes."""

    @pytest.fixture(autouse=True)
    def _patch_branch(self, monkeypatch):
        import orchestrator
        monkeypatch.setattr(orchestrator, "_current_branch", lambda: "test")
        monkeypatch.setattr(orchestrator, "_branch_mismatch", lambda st: False)

    @pytest.fixture
    def noop_gate(self, tmp_path):
        """Gate script that always returns 0."""
        gate = tmp_path / "gate.py"
        gate.write_text("import sys, json; json.load(sys.stdin); print(''); sys.exit(0)")
        return str(gate)

    def test_grill_blocked_when_at_plan_step(self, noop_gate):
        from orchestrator import hook_pre_edit_logic, GRILL_RESULT_FILENAME
        orch = {"current_step": "1.4", "phase": 1, "branch": "test"}
        data = {"tool_input": {"file_path": f"/repo/.claude/{GRILL_RESULT_FILENAME}"}}
        code = hook_pre_edit_logic(data, orch, noop_gate)
        assert code == 1, "Should block grill write when at plan step"

    def test_plan_allowed_when_at_plan_step(self, noop_gate):
        from orchestrator import hook_pre_edit_logic
        orch = {"current_step": "1.4", "phase": 1, "branch": "test"}
        data = {"tool_input": {"file_path": "/repo/docs/superpowers/plans/2026-01-01-test.md"}}
        code = hook_pre_edit_logic(data, orch, noop_gate)
        assert code != 1, "Should allow plan write when at plan step"

    def test_brief_blocked_when_at_grill_step(self, noop_gate):
        from orchestrator import hook_pre_edit_logic, BRIEF_FILENAME
        orch = {"current_step": "1.5", "phase": 1, "branch": "test"}
        data = {"tool_input": {"file_path": f"/repo/.claude/{BRIEF_FILENAME}"}}
        code = hook_pre_edit_logic(data, orch, noop_gate)
        assert code == 1, "Should block brief write when at grill step"

    def test_codex_review_blocked_when_at_grill_step(self, noop_gate):
        from orchestrator import hook_pre_edit_logic, CODEX_REVIEW_FILENAME
        orch = {"current_step": "1.5", "phase": 1, "branch": "test"}
        data = {"tool_input": {"file_path": f"/repo/.claude/{CODEX_REVIEW_FILENAME}"}}
        code = hook_pre_edit_logic(data, orch, noop_gate)
        assert code == 1, "Should block codex review write when at grill step"

    def test_grill_allowed_when_at_grill_step(self, noop_gate):
        from orchestrator import hook_pre_edit_logic, GRILL_RESULT_FILENAME
        orch = {"current_step": "1.5", "phase": 1, "branch": "test"}
        data = {"tool_input": {"file_path": f"/repo/.claude/{GRILL_RESULT_FILENAME}"}}
        code = hook_pre_edit_logic(data, orch, noop_gate)
        assert code != 1, "Should allow grill write when at grill step"

    def test_non_artifact_file_not_blocked(self, noop_gate):
        from orchestrator import hook_pre_edit_logic
        orch = {"current_step": "1.4", "phase": 1, "branch": "test"}
        data = {"tool_input": {"file_path": "/repo/.claude/some-random-note.md"}}
        code = hook_pre_edit_logic(data, orch, noop_gate)
        assert code != 1, "Non-artifact files should not be blocked"

    def test_artifact_owner_mapping(self):
        from orchestrator import _artifact_owner_step, BRIEF_FILENAME, GRILL_RESULT_FILENAME, CODEX_REVIEW_FILENAME
        assert _artifact_owner_step(f"/repo/.claude/{BRIEF_FILENAME}") == "1.3"
        assert _artifact_owner_step(f"/repo/.claude/{GRILL_RESULT_FILENAME}") == "1.5"
        assert _artifact_owner_step(f"/repo/.claude/{CODEX_REVIEW_FILENAME}") == "1.5c"
        assert _artifact_owner_step("/repo/docs/superpowers/plans/2026-test.md") == "1.4"
        assert _artifact_owner_step("/repo/docs/KNOWLEDGE.MD") == "3.6"
        assert _artifact_owner_step("/repo/src/main.rs") is None


class TestWorktreeStateIsolation:
    """Premise 1: state_home is per-worktree — different worktrees are isolated."""

    def _git(self, *args, cwd):
        subprocess.run(["git", "-C", str(cwd), *args],
                       check=True, capture_output=True, text=True)

    def test_worktree_resolves_to_separate_state_home(self, tmp_path, monkeypatch):
        import fastship_state

        monkeypatch.delenv("FASTSHIP_STATE_HOME", raising=False)
        monkeypatch.delenv("FASTSHIP_REPO_ROOT", raising=False)
        monkeypatch.delenv("FASTSHIP_SESSION", raising=False)

        main = tmp_path / "main"
        main.mkdir()
        self._git("init", "-q", cwd=main)
        self._git("config", "user.email", "t@t.io", cwd=main)
        self._git("config", "user.name", "t", cwd=main)
        (main / "README.md").write_text("x")
        self._git("add", "-A", cwd=main)
        self._git("commit", "-qm", "init", cwd=main)

        wt = tmp_path / "wt"
        self._git("worktree", "add", "-q", str(wt), "-b", "feat", cwd=main)

        monkeypatch.chdir(main)
        home_main = fastship_state.state_home()
        monkeypatch.chdir(wt)
        home_wt = fastship_state.state_home()

        assert home_wt != home_main
        assert "worktrees" in home_wt


class TestStep20StateNoop:
    """Premise 2: at step 2.0 a code-file edit writes no state in EITHER the
    orchestrator or the gate. This is what makes same-worktree parallel
    implement safe."""

    def test_orchestrator_post_edit_noop_for_code_at_2_0(self):
        from orchestrator import detect_completion_post_edit
        data = {"tool_input": {"file_path": "services/api/src/handlers/chat.rs"}}
        assert detect_completion_post_edit("2.0", data) is None

    def test_gate_does_not_treat_code_file_as_artifact(self):
        import sys, os
        sys.path.insert(0, os.path.join(
            os.path.dirname(__file__), "..", "..", "skills", "fastship", "hooks"))
        import ship_verify_gate as gate
        code = "services/api/src/handlers/chat.rs"
        assert gate.is_plan_file(code) is False
        assert gate.is_knowledge_file(code) is False


class TestAtomicSaveJson:
    def test_save_json_no_leftover_temp_files(self, tmp_path):
        import fastship_state
        target = tmp_path / "state.json"
        fastship_state.save_json(str(target), {"n": 1})
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "state.json"]
        assert leftovers == [], f"unexpected leftover files: {leftovers}"

    def test_save_json_uses_atomic_replace(self, tmp_path, monkeypatch):
        import fastship_state
        calls = []
        real_replace = os.replace
        monkeypatch.setattr(os, "replace",
                            lambda a, b: calls.append((a, b)) or real_replace(a, b))
        target = tmp_path / "s.json"
        fastship_state.save_json(str(target), {"k": "v"})
        assert calls, "save_json must use os.replace for atomicity"
        assert calls[0][1].endswith("s.json")
        assert json.loads(target.read_text())["k"] == "v"


class TestStateLock:
    def test_lock_serializes_concurrent_increments(self, tmp_path, monkeypatch):
        import threading
        import fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        counter = tmp_path / "counter.json"
        counter.write_text(json.dumps({"n": 0}))

        def bump():
            for _ in range(50):
                with fastship_state.state_lock():
                    d = json.loads(counter.read_text())
                    d["n"] += 1
                    counter.write_text(json.dumps(d))

        threads = [threading.Thread(target=bump) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert json.loads(counter.read_text())["n"] == 200

    def test_lock_is_reentrant_within_thread(self, tmp_path, monkeypatch):
        import fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        with fastship_state.state_lock():
            with fastship_state.state_lock():
                assert True


class TestRegistryConcurrency:
    def test_concurrent_session_registration_keeps_all(self, tmp_path, monkeypatch):
        import threading
        import fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        ids = [f"sess-{i:03d}" for i in range(20)]

        def register(sid):
            fastship_state.set_current_session_id(
                sid, f"req {sid}", {"current_step": "1.0"})

        threads = [threading.Thread(target=register, args=(sid,)) for sid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        sessions = fastship_state.list_sessions()
        assert set(sessions) == set(ids), f"lost: {set(ids) - set(sessions)}"


class TestGateStateLocking:
    def _import_gate(self):
        import sys, os
        sys.path.insert(0, os.path.join(
            os.path.dirname(__file__), "..", "..", "skills", "fastship", "hooks"))
        import ship_verify_gate as gate
        return gate

    def test_gate_post_edit_rmw_serialized(self, tmp_path, monkeypatch):
        import threading
        import fastship_state
        gate = self._import_gate()
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        monkeypatch.setattr(gate, "get_current_branch", lambda: "main")
        monkeypatch.setattr(gate, "require_branch_match", lambda st, br: True)
        monkeypatch.setattr(gate, "is_plan_file", lambda p: p.endswith("plan.md"))
        monkeypatch.setattr(gate, "is_knowledge_file",
                            lambda p: os.path.basename(p).upper() == "KNOWLEDGE.MD")

        tl = threading.local()
        monkeypatch.setattr(gate, "read_stdin", lambda: getattr(tl, "data", {}))

        def worker(file_path):
            tl.data = {"tool_input": {"file_path": file_path}}
            gate.gate_post_edit()

        threads = [
            threading.Thread(target=worker, args=("docs/plan.md",)),
            threading.Thread(target=worker, args=("KNOWLEDGE.md",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        st = gate.ensure_branch_state(gate.load_state(), "main")
        assert st.get("plan_ready") is True
        assert st.get("knowledge_acknowledged") is True


class TestAmbiguousSessionGuard:
    def _seed_two_active(self):
        import fastship_state
        fastship_state.set_current_session_id("alpha", "feature alpha", {"current_step": "2.0"})
        fastship_state.set_current_session_id("beta", "feature beta", {"current_step": "1.4"})

    def test_active_session_ids_excludes_done(self, tmp_path, monkeypatch):
        import fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        fastship_state.set_current_session_id("a", "ra", {"current_step": "2.0"})
        fastship_state.set_current_session_id("b", "rb", {"current_step": "done"})
        assert fastship_state.active_session_ids() == ["a"]

    def test_ambiguous_when_two_active_no_pin(self, tmp_path, monkeypatch):
        import orchestrator
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        monkeypatch.delenv("FASTSHIP_SESSION", raising=False)
        self._seed_two_active()
        assert orchestrator._hook_session_ambiguous() is True

    def test_not_ambiguous_when_pinned(self, tmp_path, monkeypatch):
        import orchestrator
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        monkeypatch.setenv("FASTSHIP_SESSION", "alpha")
        self._seed_two_active()
        assert orchestrator._hook_session_ambiguous() is False

    def test_post_bash_no_advance_when_ambiguous(self, tmp_path, monkeypatch, capsys):
        import orchestrator
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        monkeypatch.delenv("FASTSHIP_SESSION", raising=False)
        self._seed_two_active()
        rc = orchestrator.hook_post_bash_logic(
            {"tool_input": {"command": "x"}}, hook_state={"request_classified": True})
        out = capsys.readouterr().out
        assert rc == 0
        assert "多个活跃 session" in out

    def test_pre_edit_failopen_via_ambiguous_param(self, capsys):
        import orchestrator
        orch_state = {"current_step": "1.4", "phase": 1, "branch": None}
        rc_code = orchestrator.hook_pre_edit_logic(
            {"tool_input": {"file_path": "src/app.rs"}}, orch_state,
            "/nonexistent-gate.py", ambiguous=True)
        assert rc_code == 0
        rc_state = orchestrator.hook_pre_edit_logic(
            {"tool_input": {"file_path": "x/fastship/orchestrator.json"}},
            orch_state, "/nonexistent-gate.py", ambiguous=True)
        assert rc_state == 1

    def test_pre_edit_default_not_ambiguous_preserves_blocking(self):
        import orchestrator
        orch_state = {"current_step": "1.4", "phase": 1, "branch": None}
        rc = orchestrator.hook_pre_edit_logic(
            {"tool_input": {"file_path": "src/app.rs"}}, orch_state, "/nonexistent-gate.py")
        assert rc == 1


class TestStartSecondSessionRefusal:
    def test_other_active_sessions_excludes_self_and_done(self, tmp_path, monkeypatch):
        import orchestrator, fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        fastship_state.set_current_session_id("self", "mine", {"current_step": "2.0"})
        fastship_state.set_current_session_id("other", "theirs", {"current_step": "1.4"})
        fastship_state.set_current_session_id("old", "done", {"current_step": "done"})
        assert orchestrator._other_active_sessions("self") == ["other"]

    def test_blocking_message_lists_other_and_mentions_shared(self, tmp_path, monkeypatch):
        import orchestrator, fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        fastship_state.set_current_session_id("other", "theirs", {"current_step": "1.4"})
        msg = orchestrator._blocking_active_session_msg("newcomer")
        assert msg is not None
        assert "other" in msg
        assert "--shared" in msg
        assert "worktree" in msg.lower()

    def test_no_block_when_no_other_active(self, tmp_path, monkeypatch):
        import orchestrator, fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        fastship_state.set_current_session_id("solo", "only", {"current_step": "1.0"})
        assert orchestrator._blocking_active_session_msg("solo") is None


class TestImplementVerdictsPath:
    def test_path_is_under_session_dir(self, tmp_path, monkeypatch):
        import fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        p_a = fastship_state.implement_verdicts_path("alpha")
        p_b = fastship_state.implement_verdicts_path("beta")
        assert p_a.endswith("sessions/alpha/implement-verdicts.md")
        assert p_b.endswith("sessions/beta/implement-verdicts.md")
        assert p_a != p_b

    def test_path_follows_current_session_when_unspecified(self, tmp_path, monkeypatch):
        import fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        monkeypatch.setenv("FASTSHIP_SESSION", "gamma")
        assert fastship_state.implement_verdicts_path().endswith(
            "sessions/gamma/implement-verdicts.md")


class TestStep20Contract:
    def _instr(self):
        from orchestrator import STEPS
        s = next(s for s in STEPS if s.id == "2.0")
        return s.instruction({}) if callable(s.instruction) else s.instruction

    def test_dependency_aware_partition(self):
        i = self._instr()
        assert "不相交" in i and "parallel" in i

    def test_shared_worktree_edit_only_no_commit(self):
        i = self._instr()
        assert "不各自 commit" in i or "不要各自 commit" in i
        assert "merge" not in i.lower()  # merge-back removed

    def test_no_parallel_tests_during_implement(self):
        i = self._instr()
        assert "编译检查" in i
        assert "测试套件" in i or "E2E" in i

    def test_conditional_workflow_and_sequential_fallback(self):
        i = self._instr()
        assert "≥2" in i or ">=2" in i
        assert "串行" in i

    def test_session_scoped_verdict_ledger_feeds_2_5(self):
        i = self._instr()
        assert "implement-verdicts" in i
        assert "2.5" in i
