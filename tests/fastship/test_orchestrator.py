import json
import os
import subprocess
import sys
import time
import tempfile
from datetime import datetime
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship'))


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

        assert fastship_state.repo_root() == str(project.resolve())


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
        assert validate_brief({"brief_path": str(f)}, {})[0] is True

    def test_brief_fail_missing_section(self, tmp_path):
        from orchestrator import validate_brief
        f = tmp_path / "brief.md"
        f.write_text("### 涉及模块\nx\n" + "p " * 100)
        ok, msg = validate_brief({"brief_path": str(f)}, {})
        assert ok is False

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
        ok, _ = validate_plan({}, {"plan_ready": True, "plan_file": str(plan)})
        assert ok is True

    def test_plan_fail_no_signature(self, tmp_path, monkeypatch):
        from orchestrator import validate_plan
        plan_dir = tmp_path / "docs" / "superpowers" / "plans"
        plan_dir.mkdir(parents=True)
        plan = plan_dir / "2026-05-18-feat.md"
        plan.write_text("# My hand-written plan\n## Steps\n1. Do stuff\n")
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, msg = validate_plan({}, {"plan_ready": True, "plan_file": str(plan)})
        assert ok is False
        assert "签名" in msg or "writing-plans" in msg

    def test_plan_fail_no_file(self, tmp_path, monkeypatch):
        from orchestrator import validate_plan
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, _ = validate_plan({}, {})
        assert ok is False

    def test_grill_pass(self, tmp_path, monkeypatch):
        from orchestrator import validate_grill
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
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, _ = validate_grill({"artifacts": {}}, {})
        assert ok is True

    def test_grill_fail_no_file(self, tmp_path, monkeypatch):
        from orchestrator import validate_grill
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, _ = validate_grill({"artifacts": {}}, {})
        assert ok is False

    def test_grill_fail_missing_section(self, tmp_path, monkeypatch):
        from orchestrator import validate_grill
        grill = tmp_path / ".claude" / ".fastship-grill-result.md"
        grill.parent.mkdir(parents=True)
        grill.write_text("## 拷问记录\nstuff\n" + "x " * 200)
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, msg = validate_grill({"artifacts": {}}, {})
        assert ok is False
        assert "修订" in msg or "结论" in msg

    def test_grill_fail_too_short(self, tmp_path, monkeypatch):
        from orchestrator import validate_grill
        grill = tmp_path / ".claude" / ".fastship-grill-result.md"
        grill.parent.mkdir(parents=True)
        grill.write_text("## 拷问\n## 修订\n## 结论\nok")
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, msg = validate_grill({"artifacts": {}}, {})
        assert ok is False
        assert "300B" in msg

    def test_confirm_pass(self):
        from orchestrator import validate_user_confirm
        assert validate_user_confirm({"artifacts": {"user_confirmed": True}}, {})[0] is True


class TestValidatorsPhase2:
    def test_execute_pass(self):
        from orchestrator import validate_execute
        assert validate_execute({}, {})[0] is True


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

    def test_report_pass(self, tmp_path):
        from orchestrator import validate_e2e_report
        f = tmp_path / "report.md"
        f.write_text("## Report\n" + "x " * 150)
        assert validate_e2e_report({"report_path": str(f)}, {})[0] is True

    def test_report_fail_small(self, tmp_path):
        from orchestrator import validate_e2e_report
        f = tmp_path / "report.md"
        f.write_text("short")
        assert validate_e2e_report({"report_path": str(f)}, {})[0] is False

    def test_knowledge_pass(self):
        from orchestrator import validate_knowledge
        assert validate_knowledge({}, {"knowledge_acknowledged": True})[0] is True

    def test_loop_pass(self):
        from orchestrator import validate_loop_record
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


class TestValidatorsCodexFallback:
    def test_plan_fs_fallback(self, tmp_path, monkeypatch):
        from orchestrator import validate_plan
        plan_dir = tmp_path / "docs" / "superpowers" / "plans"
        plan_dir.mkdir(parents=True)
        (plan_dir / "2026-05-18-feat.md").write_text(
            "# Plan\n> **For agentic workers:** REQUIRED\n"
            "**Goal:** x\n- [ ] **Step 1:** y\n"
        )
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, msg = validate_plan({}, {})
        assert ok is True
        assert "feat" in msg

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

    def test_e2e_run_fs_fallback(self, tmp_path, monkeypatch):
        from orchestrator import validate_e2e_run
        result_file = tmp_path / "e2e_result.json"
        result_file.write_text('{"turns": []}')
        monkeypatch.setattr("orchestrator.E2E_RESULT_PATH", str(result_file))
        ok, _ = validate_e2e_run({}, {})
        assert ok is True

    def test_knowledge_fs_fallback(self, tmp_path, monkeypatch):
        from orchestrator import validate_knowledge
        km = tmp_path / "KNOWLEDGE.md"
        km.write_text("## 2026-05-18 — lesson")
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        orch = {"started_at": "2020-01-01T00:00:00"}
        ok, _ = validate_knowledge(orch, {})
        assert ok is True

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
        assert len(STEPS) == 16

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
            assert len(s.instruction) > 30, f"{s.id} instruction too short"

    def test_required_ids_present(self):
        from orchestrator import STEPS
        ids = {s.id for s in STEPS}
        for expected in ["1.0", "1.1", "1.2", "1.3", "1.3d", "1.4", "1.5", "1.6",
                         "2.0", "3.0", "3.1", "3.2", "3.3", "3.4", "3.5", "3.6"]:
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

        # 2.0 + 3.0: manual done
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
        report = tmp_path / "report.md"
        report.write_text("## Report\n" + "x " * 150)
        st["report_path"] = str(report)
        save_orch_state(st, orch_file)
        hook_post_edit_logic(
            data={"tool_input": {"file_path": str(report)}},
            orch_path=orch_file)
        st = reload()
        assert st["current_step"] == "3.4"

        # 3.4: gate (auto)
        hook_post_bash_logic(
            data={"tool_input": {"command": "python3 tests/e2e_gate.py --result /tmp/e2e.json"}},
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
            "completed_steps": ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6",
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
            "completed_steps": ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6",
                                "2.0", "3.0", "3.1", "3.2", "3.3", "3.4"],
            "skipped_steps": ["1.3d"], "request_type": "feature",
            "artifacts": {"loop_outcome": "fail", "loop_decision": "continue"},
        }
        _handle_loop_decision(st)
        assert st["current_step"] == "3.1"
        assert "3.1" not in st["completed_steps"]
        assert "1.0" in st["completed_steps"]

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
