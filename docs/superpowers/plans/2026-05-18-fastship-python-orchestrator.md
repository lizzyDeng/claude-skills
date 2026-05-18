# Fastship Python Orchestrator Implementation Plan (v2.1 — Hook Entry + Codex Compatible)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace SKILL.md's 852-line prompt-based orchestration with a Python state machine. In Claude Code it's the hook entry point (auto-detection + blocking); in Codex/other agents it's a CLI workflow (manual `done` with artifact validation). Same orchestrator, same validators, two modes.

**Architecture:** `orchestrator.py` has three modes: **hook mode** (Claude Code — called on every tool use via `settings.local.json`), **CLI mode** (any agent — `start`/`next`/`done`/`status`/`reset`), and **check mode** (`done` without hooks — validates by scanning file system directly). Validators use dual-path: hook state first (fast, Claude Code), filesystem fallback second (Codex/any agent). 15 steps total: Claude Code auto-detects 11 (including grill result file); Codex needs manual `done` for all 15 but gets the same hard validation on artifacts.

**Tech Stack:** Python 3.10+ (stdlib only), JSON state files, existing hook + gate infrastructure

---

## Architecture Overview

```
Claude Code (hook 模式 — 最强执行力):
  hooks → orchestrator.py (主动驱动 + 被动拦截)
           ├── pre_edit/pre_bash: 不在正确 phase → BLOCK + 打印步骤指令
           ├── post_edit/post_bash: 自动检测步骤完成 → advance
           └── 内部 subprocess 调用 ship_verify_gate.py (低层 gate)
  11 步自动检测 + 4 步手动 done
  Claude 无法绕过（hooks 强制）

Codex / 其他 Agent (CLI 模式 — 降级但仍有硬验证):
  Agent 手动调用 → orchestrator.py start / next / done / status
           ├── done: 双路径验证（hook state → filesystem fallback）
           ├── next: 打印当前步骤指令
           └── 无 Phase 1 代码阻断（靠 agent 遵循 next 指令）
  15 步全部手动 done，但 done 仍然做硬性 artifact 验证
```

## Design Decisions

### 1. 双模 orchestrator：hook 入口 + CLI 工具

Claude Code: hooks 是唯一不可绕过的执行路径 → orchestrator 作为 hook = 强制。
Codex/其他: 没有 hooks → orchestrator 作为 CLI 工具，agent 需自愿调用 `next`/`done`。
同一个 `orchestrator.py`，同一套 validators，两种调用方式。

### 2. 委托模式：orchestrator 包裹 ship_verify_gate

不重写 ship_verify_gate.py（1270 行），orchestrator 通过 subprocess 调用它：

```python
def delegate_to_gate(action, data):
    proc = subprocess.run(
        ["python3", gate_script_path(), action],
        input=json.dumps(data), capture_output=True, text=True, timeout=10
    )
    print(proc.stdout, end="")
    return proc.returncode
```

执行顺序：
- pre_* hooks: orchestrator 先检查 → 通过后 delegate to gate
- post_* hooks: gate 先执行（更新 hook state）→ orchestrator 再读 state 做检测

### 3. 双路径验证（Codex 兼容的关键）

每个 validator 先检查 hook state（Claude Code 自动更新），fallback 到直接检查文件系统（Codex 环境 hook state 不存在时）：

```python
def validate_plan(orch, hook):
    # 路径 1: hook state (Claude Code, 自动更新)
    if hook.get("plan_ready"):
        return True, f"plan: {hook.get('plan_file')}"
    # 路径 2: filesystem scan (Codex fallback)
    plans = glob.glob(os.path.join(_repo_root(), "docs/superpowers/plans/*.md"))
    if plans:
        newest = max(plans, key=os.path.getmtime)
        return True, f"plan: {newest}"
    return False, "plan 文件未找到"
```

这意味着 Codex agent 只要产出了正确的 artifact（plan 文件、brief 文件、KNOWLEDGE.md），`done` 就能通过验证，不需要 hook state。

### 4. 自动检测 vs 手动 done

| 检测方式 | 步骤 | Claude Code | Codex |
|---------|------|------------|-------|
| **post_bash 自动** | 1.0, 1.1, 1.3d, 3.1, 3.2, 3.4, 3.5 | auto | manual `done` |
| **post_edit 自动** | 1.3, 1.4, 1.5, 3.3, 3.6 | auto | manual `done` |
| **手动 done** | 1.2, 1.6, 2.0, 3.0 | manual | manual |

### 5. 无 session 时回退

orchestrator state 不存在时，hook 模式完全回退到 ship_verify_gate 原有行为。CLI 模式返回"没有活跃 session"。

### 6. Orchestrator state 独立于 hook state

- Orchestrator: `.claude/.fastship-orchestrator-state.json`（步骤跟踪）
- Hook gate: `.claude/.ship-verify-state.json`（gate 标志，Claude Code only）
- Orchestrator 只读 hook state，不写入

## File Structure

```
skills/fastship/
├── orchestrator.py          ← CREATE: hook entry + state machine + CLI (~900 lines)
├── SKILL.md                 ← MODIFY: thin wrapper (~80 lines, was 852)
├── INSTALL.md               ← MODIFY: updated installation
├── hooks/
│   └── ship_verify_gate.py  ← NO CHANGE (called as subprocess)
└── e2e/                     ← NO CHANGE

tests/fastship/
└── test_orchestrator.py     ← CREATE: unit + integration tests (~400 lines)

.claude/commands/
├── fastship.md              ← MODIFY: sync with SKILL.md
└── fastship-setup.md        ← MODIFY: add orchestrator + hook config change
```

## Step Definitions (15 steps)

### Phase 1: Brainstorm + Plan + Grill

| Step | Name | Claude Code auto | Codex done | Validator (hook → fs fallback) |
|------|------|-----------------|------------|-------------------------------|
| 1.0 | 需求分类 | post_bash: `classify` | `done` | hook `request_classified` → fs: `.ship-verify-state.json` has field |
| 1.1 | 上下文+recall | post_bash: `knowledge_recall` | `done` | hook `knowledge_recall_done` → fs: state file has field |
| 1.2 | 并行 Explore | — | `done --agents N` | artifact: agents count ≥ 3 |
| 1.3 | Context Brief | post_edit: brief file | `done` | file: `.fastship-brief.md` exists + 4 sections + ≥200B |
| 1.3d | Bug 诊断 | post_bash: `fix_verified` | `done` | hook `bug_diagnosis_done` → fs: state file (conditional: bugfix) |
| 1.4 | 写计划 | post_edit: plan file | `done` | **file: plan 存在 + writing-plans 签名** (hook/fs dual) |
| 1.5 | Grill | post_edit: grill result | `done` | **file: `.fastship-grill-result.md` ≥300B + 拷问/修订/结论** |
| 1.6 | 用户确认 | — | `done --user-confirmed` | artifact: flag |

### Phase 2: Execution

| Step | Name | Claude Code auto | Codex done | Validator |
|------|------|-----------------|------------|-----------|
| 2.0 | 执行计划 | — | `done` | soft: sequencing |

### Phase 3: Verification Loop

| Step | Name | Claude Code auto | Codex done | Validator (hook → fs fallback) |
|------|------|-----------------|------------|-------------------------------|
| 3.0 | 冒烟测试 | — | `done` | soft: sequencing |
| 3.1 | 项目测试 | post_bash: test pass | `done` | hook `test_passed` → **fs: soft (sequencing)** |
| 3.2 | E2E Runner | post_bash: e2e cmd | `done` | hook `e2e_executed` → **fs: `/tmp/e2e_result.json` exists + fresh** |
| 3.3 | E2E 报告 | post_edit: report file | `done --report <path>` | file: exists + ≥200B |
| 3.4 | E2E Gate | post_bash: `e2e_gate` | `done` | soft: sequencing |
| 3.5 | Loop Record | post_bash: `loop_record` | `done --outcome pass/fail` | hook `loop_count` → **fs: state file loop_count** |
| 3.6 | KNOWLEDGE 闭环 | post_edit: KNOWLEDGE.md | `done` | hook `knowledge_acknowledged` → **fs: KNOWLEDGE.md mtime recent** |

**fs fallback 标注**（粗体）= Codex 模式下不依赖 hook state 的替代验证路径。

### Loop Routing (after 3.5)

- `last_loop_outcome=pass` → advance to 3.6
- `last_loop_outcome=fail`:
  - Orchestrator reads reflection file's Decision field
  - `continue` → rewind to 3.1 (clear 3.x from completed)
  - `escalate` → rewind to 1.0 (full reset)
  - `stop` → enter "stopped" state
  - `loop_count ≥ 3` → forced stop

---

## Task 1: Core Infrastructure

**Files:**
- Create: `skills/fastship/orchestrator.py`
- Create: `tests/fastship/test_orchestrator.py`

- [ ] **Step 1: Write failing tests for state management + gate delegation**

```python
# tests/fastship/test_orchestrator.py
import json
import os
import sys
import tempfile
from datetime import datetime
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship'))


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

    def test_load_branch_mismatch_returns_none(self, tmp_path, monkeypatch):
        from orchestrator import save_orch_state, load_orch_state
        f = str(tmp_path / "state.json")
        st = {"requirement": "test", "current_step": "1.0", "branch": "feat/old"}
        save_orch_state(st, f)
        monkeypatch.setattr("orchestrator._current_branch", lambda: "main")
        assert load_orch_state(f) is None

    def test_load_branch_match_returns_state(self, tmp_path, monkeypatch):
        from orchestrator import save_orch_state, load_orch_state
        f = str(tmp_path / "state.json")
        st = {"requirement": "test", "current_step": "1.0", "branch": "feat/x"}
        save_orch_state(st, f)
        monkeypatch.setattr("orchestrator._current_branch", lambda: "feat/x")
        loaded = load_orch_state(f)
        assert loaded is not None
        assert loaded["requirement"] == "test"


class TestDelegation:
    def test_delegate_to_gate_returns_exit_code(self, tmp_path):
        from orchestrator import delegate_to_gate
        # Create a fake gate script that exits with 0
        fake_gate = tmp_path / "fake_gate.py"
        fake_gate.write_text("import sys, json; json.load(sys.stdin); print('ok'); sys.exit(0)")
        code, stdout = delegate_to_gate(str(fake_gate), "status", {})
        assert code == 0
        assert "ok" in stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement core infrastructure**

```python
# skills/fastship/orchestrator.py
#!/usr/bin/env python3
"""
fastship_orchestrator.py — Hook entry point + state machine for /fastship.

Dual-mode:
  Hook mode (called by settings.local.json hooks, reads stdin):
    pre_edit / pre_bash / post_edit / post_bash
  CLI mode (called by Claude for manual steps):
    start / done / next / status / reset

Delegates to ship_verify_gate.py (subprocess) for low-level gate enforcement.
"""

import sys
import os
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable


# ━━━━━━━━━━━━ State Management ━━━━━━━━━━━━

def _repo_root():
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else os.getcwd()
    except Exception:
        return os.getcwd()


def orch_state_path():
    return os.path.join(_repo_root(), ".claude", ".fastship-orchestrator-state.json")


def hook_state_path():
    return os.path.join(_repo_root(), ".claude", ".ship-verify-state.json")


def gate_script_path():
    return os.path.join(_repo_root(), ".claude", "hooks", "ship_verify_gate.py")


def _current_branch() -> Optional[str]:
    try:
        r = subprocess.run(["git", "branch", "--show-current"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def empty_orchestrator_state(requirement: str) -> dict:
    return {
        "requirement": requirement,
        "request_type": None,
        "current_step": "1.0",
        "completed_steps": [],
        "skipped_steps": [],
        "phase": 1,
        "branch": _current_branch(),
        "brief_path": None,
        "plan_path": None,
        "report_path": None,
        "started_at": datetime.now().isoformat(),
        "loop_count": 0,
        "artifacts": {},
    }


def save_orch_state(st: dict, path: str = None):
    p = path or orch_state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(st, f, indent=2)


def load_orch_state(path: str = None) -> Optional[dict]:
    """Load orchestrator state. Returns None if no state or branch mismatch."""
    p = path or orch_state_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            st = json.load(f)
    except Exception:
        return None
    # Branch guard: if branch changed, treat as no session
    saved_branch = st.get("branch")
    if saved_branch is not None:
        current = _current_branch()
        if current and current != saved_branch:
            return None
    return st


def load_hook_state(path: str = None) -> dict:
    p = path or hook_state_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


# ━━━━━━━━━━━━ Gate Delegation ━━━━━━━━━━━━

def delegate_to_gate(gate_path: str, action: str, data: dict) -> tuple[int, str]:
    """Call ship_verify_gate.py as subprocess, piping hook data via stdin.
    Returns (exit_code, stdout)."""
    if not os.path.exists(gate_path):
        return 0, ""
    try:
        proc = subprocess.run(
            [sys.executable, gate_path, action],
            input=json.dumps(data),
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode, proc.stdout
    except Exception:
        return 0, ""


def read_stdin() -> dict:
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


# ━━━━━━━━━━━━ Step Dataclass ━━━━━━━━━━━━

@dataclass
class Step:
    id: str
    name: str
    phase: int
    instruction: str
    validator: Callable[[dict, dict], tuple[bool, str]]
    done_flags: list[str] = field(default_factory=list)
    conditional: Optional[str] = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add skills/fastship/orchestrator.py tests/fastship/test_orchestrator.py
git commit -m "feat(fastship): orchestrator v2 core — state, delegation, Step dataclass"
```

---

## Task 2: Validators

**Files:**
- Modify: `skills/fastship/orchestrator.py`
- Modify: `tests/fastship/test_orchestrator.py`

- [ ] **Step 1: Write failing tests for all validators**

```python
# tests/fastship/test_orchestrator.py (append)

class TestValidatorsPhase1:
    def test_classify_pass(self):
        from orchestrator import validate_classify
        assert validate_classify({}, {"request_classified": True})[0] is True

    def test_classify_fail(self):
        from orchestrator import validate_classify
        assert validate_classify({}, {})[0] is False

    def test_recall_pass(self):
        from orchestrator import validate_recall
        assert validate_recall({}, {"knowledge_recall_done": True})[0] is True

    def test_recall_fail(self):
        from orchestrator import validate_recall
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

    def test_diagnosis_fail_bugfix(self):
        from orchestrator import validate_diagnosis
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
        ok, msg = validate_grill({"artifacts": {}}, {})
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
    """Filesystem fallback tests — simulate Codex mode (no hook state)."""

    def test_plan_fs_fallback(self, tmp_path, monkeypatch):
        from orchestrator import validate_plan
        plan_dir = tmp_path / "docs" / "superpowers" / "plans"
        plan_dir.mkdir(parents=True)
        (plan_dir / "2026-05-18-feat.md").write_text(
            "# Plan\n> **For agentic workers:** REQUIRED\n"
            "**Goal:** x\n- [ ] **Step 1:** y\n"
        )
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, msg = validate_plan({}, {})  # empty hook = Codex
        assert ok is True
        assert "feat" in msg

    def test_plan_fs_fallback_no_file(self, tmp_path, monkeypatch):
        from orchestrator import validate_plan
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        ok, _ = validate_plan({}, {})
        assert ok is False

    def test_classify_gate_state_fallback(self, tmp_path, monkeypatch):
        from orchestrator import validate_classify
        # Write a gate state file directly (as classify CLI would)
        gate_file = tmp_path / ".claude" / ".ship-verify-state.json"
        gate_file.parent.mkdir(parents=True)
        gate_file.write_text('{"request_classified": true, "request_type": "feature"}')
        monkeypatch.setattr("orchestrator.hook_state_path", lambda: str(gate_file))
        ok, msg = validate_classify({}, {})  # empty hook = Codex
        assert ok is True
        assert "feature" in msg

    def test_e2e_run_fs_fallback(self, tmp_path, monkeypatch):
        from orchestrator import validate_e2e_run
        import time
        result_file = tmp_path / "e2e_result.json"
        result_file.write_text('{"turns": []}')
        # Patch the result path check
        monkeypatch.setattr("orchestrator.E2E_RESULT_PATH", str(result_file))
        ok, msg = validate_e2e_run({}, {})  # empty hook
        assert ok is True

    def test_knowledge_fs_fallback(self, tmp_path, monkeypatch):
        from orchestrator import validate_knowledge
        km = tmp_path / "KNOWLEDGE.md"
        km.write_text("## 2026-05-18 — lesson")
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        # started_at must be before KNOWLEDGE.md mtime
        orch = {"started_at": "2020-01-01T00:00:00"}
        ok, _ = validate_knowledge(orch, {})  # empty hook
        assert ok is True

    def test_knowledge_fs_fallback_stale(self, tmp_path, monkeypatch):
        from orchestrator import validate_knowledge
        import os, time
        km = tmp_path / "KNOWLEDGE.md"
        km.write_text("## old lesson")
        # Set mtime to past
        old_time = time.time() - 7200
        os.utime(str(km), (old_time, old_time))
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
        # Session started after the file was last modified
        orch = {"started_at": datetime.now().isoformat()}
        ok, _ = validate_knowledge(orch, {})
        assert ok is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestValidatorsPhase1 -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement all validators (dual-path: hook state → filesystem fallback)**

```python
# skills/fastship/orchestrator.py (append after Step dataclass)

import glob as _glob
import time as _time

# ━━━━━━━━━━━━ Validators (dual-path: hook state → fs fallback) ━━━━━━━━━━━━


def _read_gate_state_file() -> dict:
    """Directly read ship_verify_gate state file (for Codex fallback)."""
    p = hook_state_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


def validate_classify(orch: dict, hook: dict) -> tuple[bool, str]:
    # Path 1: hook state (Claude Code)
    if hook.get("request_classified"):
        return True, f"classified: {hook.get('request_type')}"
    # Path 2: gate state file (Codex — classify CLI writes directly to state file)
    gate = _read_gate_state_file()
    if gate.get("request_classified"):
        return True, f"classified: {gate.get('request_type')} (via state file)"
    return False, "运行 classify CLI 注册需求类型"


def validate_recall(orch: dict, hook: dict) -> tuple[bool, str]:
    if hook.get("knowledge_recall_done"):
        return True, f"recall done (hits={hook.get('knowledge_recall_count', 0)})"
    gate = _read_gate_state_file()
    if gate.get("knowledge_recall_done"):
        return True, f"recall done (hits={gate.get('knowledge_recall_count', 0)}, via state file)"
    return False, "运行 knowledge_recall CLI"


def validate_explore(orch: dict, hook: dict) -> tuple[bool, str]:
    n = orch.get("artifacts", {}).get("explore_agents", 0)
    if n >= 3:
        return True, f"{n} agents dispatched"
    return False, f"需要 ≥3 个 Explore subagent (当前: {n})"


def validate_brief(orch: dict, hook: dict) -> tuple[bool, str]:
    path = orch.get("brief_path")
    # Fallback: check default location
    if not path or not os.path.exists(path):
        default = os.path.join(_repo_root(), ".claude", BRIEF_FILENAME)
        if os.path.exists(default):
            path = default
    if not path or not os.path.exists(path):
        return False, "Brief 文件不存在"
    try:
        content = open(path, encoding="utf-8").read()
    except Exception:
        return False, f"无法读取: {path}"
    required = ["涉及模块", "现有测试", "历史变更", "历史教训"]
    missing = [s for s in required if s not in content]
    if missing:
        return False, f"Brief 缺少: {', '.join(missing)}"
    if len(content) < 200:
        return False, f"Brief 太短 ({len(content)}B < 200B)"
    return True, "brief validated"


def validate_diagnosis(orch: dict, hook: dict) -> tuple[bool, str]:
    if orch.get("request_type") != "bugfix":
        return True, "非 bugfix，跳过"
    if hook.get("bug_diagnosis_done"):
        return True, "D1+D2+D3 完成"
    gate = _read_gate_state_file()
    if gate.get("bug_diagnosis_done"):
        return True, "D1+D2+D3 完成 (via state file)"
    return False, "Bug 诊断 Gate 未完成"


PLAN_SIGNATURE_MARKERS = [
    "For agentic workers",
    "**Goal:**",
    "- [ ] **Step",
]


def validate_plan(orch: dict, hook: dict) -> tuple[bool, str]:
    # Find the plan file (hook state or filesystem scan)
    plan_path = None
    if hook.get("plan_ready") and hook.get("plan_file"):
        candidate = hook["plan_file"]
        if not os.path.isabs(candidate):
            candidate = os.path.join(_repo_root(), candidate)
        if os.path.exists(candidate):
            plan_path = candidate
    if not plan_path:
        plans = _glob.glob(os.path.join(_repo_root(), "docs", "superpowers", "plans", "*.md"))
        if plans:
            plan_path = max(plans, key=os.path.getmtime)
    if not plan_path:
        return False, "plan 文件未检测到 (docs/superpowers/plans/*.md)"

    # Signature check: writing-plans skill produces specific markers
    try:
        content = open(plan_path, encoding="utf-8").read()
    except Exception:
        return False, f"无法读取 plan: {plan_path}"
    missing = [m for m in PLAN_SIGNATURE_MARKERS if m not in content]
    if missing:
        return False, (
            f"plan 文件存在但缺少 writing-plans 签名: {', '.join(missing)}。"
            f"必须通过 Skill(skill='writing-plans') 产出，不能手写。"
        )
    return True, f"plan: {os.path.relpath(plan_path, _repo_root())}"


GRILL_RESULT_FILENAME = ".fastship-grill-result.md"
GRILL_REQUIRED_SECTIONS = ["拷问", "修订", "结论"]


def validate_grill(orch: dict, hook: dict) -> tuple[bool, str]:
    # Check for grill result file
    path = orch.get("artifacts", {}).get("grill_result_path")
    if not path:
        path = os.path.join(_repo_root(), ".claude", GRILL_RESULT_FILENAME)
    if not os.path.exists(path):
        return False, (
            f"Grill 结果文件不存在: {GRILL_RESULT_FILENAME}。"
            f"grill-me 完成后用 Write 写摘要到 .claude/{GRILL_RESULT_FILENAME}"
        )
    try:
        content = open(path, encoding="utf-8").read()
    except Exception:
        return False, f"无法读取: {path}"
    if len(content) < 300:
        return False, f"Grill 摘要太短 ({len(content)}B < 300B)，需包含拷问过程和修订记录"
    missing = [s for s in GRILL_REQUIRED_SECTIONS if s not in content]
    if missing:
        return False, f"Grill 摘要缺少章节: {', '.join(missing)}"
    return True, "grill validated"


def validate_user_confirm(orch: dict, hook: dict) -> tuple[bool, str]:
    if orch.get("artifacts", {}).get("user_confirmed"):
        return True, "confirmed"
    return False, "等待用户确认"


def validate_execute(orch: dict, hook: dict) -> tuple[bool, str]:
    return True, "sequencing"


def validate_smoke(orch: dict, hook: dict) -> tuple[bool, str]:
    return True, "sequencing"


def validate_tests(orch: dict, hook: dict) -> tuple[bool, str]:
    # Path 1: hook state
    if hook.get("test_passed"):
        return True, f"tests passed ({hook.get('test_tool', '?')})"
    # Path 2: gate state file (Codex — ship_verify_gate post_bash updates this)
    gate = _read_gate_state_file()
    if gate.get("test_passed"):
        return True, f"tests passed ({gate.get('test_tool', '?')}, via state file)"
    # Path 3: soft pass in pure CLI mode (no hooks at all → sequencing only)
    # This is the weakest link for Codex — no artifact to verify
    return False, "项目测试未通过"


def validate_e2e_run(orch: dict, hook: dict) -> tuple[bool, str]:
    if hook.get("e2e_executed"):
        return True, "e2e executed"
    # Filesystem fallback: check e2e result file
    result_path = E2E_RESULT_PATH
    if os.path.exists(result_path):
        age = _time.time() - os.path.getmtime(result_path)
        if age < 3600:  # less than 1 hour old
            return True, f"e2e result found ({int(age)}s ago)"
    gate = _read_gate_state_file()
    if gate.get("e2e_executed"):
        return True, "e2e executed (via state file)"
    return False, "E2E Runner 未执行"


def validate_e2e_report(orch: dict, hook: dict) -> tuple[bool, str]:
    path = orch.get("report_path")
    if not path or not os.path.exists(path):
        return False, "报告文件不存在"
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if size < 200:
        return False, f"报告太短 ({size}B < 200B)"
    return True, f"report: {path}"


def validate_e2e_gate(orch: dict, hook: dict) -> tuple[bool, str]:
    return True, "sequencing"


def validate_loop_record(orch: dict, hook: dict) -> tuple[bool, str]:
    outcome = orch.get("artifacts", {}).get("loop_outcome")
    if not outcome:
        return False, "未记录结果 (done --outcome pass|fail)"
    if outcome == "pass":
        return True, "pass"
    decision = orch.get("artifacts", {}).get("loop_decision")
    if not decision:
        return False, "fail 但未给 decision (done --outcome fail --decision continue|escalate|stop)"
    return True, f"fail → {decision}"


def validate_knowledge(orch: dict, hook: dict) -> tuple[bool, str]:
    if hook.get("knowledge_acknowledged"):
        return True, "done"
    # Filesystem fallback: check KNOWLEDGE.md modified after session start
    root = _repo_root()
    km = os.path.join(root, "KNOWLEDGE.md")
    started_at = orch.get("started_at")
    if started_at and os.path.exists(km):
        try:
            km_mtime = os.path.getmtime(km)
            session_start = datetime.fromisoformat(started_at).timestamp()
            if km_mtime > session_start:
                return True, f"KNOWLEDGE.md modified after session start"
        except (ValueError, OSError):
            pass
    gate = _read_gate_state_file()
    if gate.get("knowledge_acknowledged"):
        return True, "done (via state file)"
    return False, "KNOWLEDGE.md 未表态"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add skills/fastship/orchestrator.py tests/fastship/test_orchestrator.py
git commit -m "feat(fastship): all 15 step validators"
```

---

## Task 3: Step Definitions + Auto-Detection Patterns

**Files:**
- Modify: `skills/fastship/orchestrator.py`
- Modify: `tests/fastship/test_orchestrator.py`

- [ ] **Step 1: Write failing tests for STEPS list and detection**

```python
# tests/fastship/test_orchestrator.py (append)

class TestSteps:
    def test_step_count(self):
        from orchestrator import STEPS
        assert len(STEPS) == 15

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
        step_id = detect_completion_post_bash("1.0", data, hook)
        assert step_id == "1.0"

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

    def test_detect_knowledge_post_edit(self):
        from orchestrator import detect_completion_post_edit
        data = {"tool_input": {"file_path": "/proj/KNOWLEDGE.md"}}
        assert detect_completion_post_edit("3.6", data) == "3.6"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestSteps -v`
Expected: FAIL

- [ ] **Step 3: Implement STEPS list**

```python
# skills/fastship/orchestrator.py (append)

# ━━━━━━━━━━━━ Step Definitions ━━━━━━━━━━━━

STEPS = [
    Step("1.0", "需求分类", 1, validator=validate_classify,
         instruction="""分析用户需求，执行分类：
  python3 .claude/hooks/ship_verify_gate.py classify --type <bugfix|feature|refactor|optimize>

  bugfix = 报错/数据不对/线上问题    feature = 新功能
  refactor = 重构/规范               optimize = 性能/体验
  🔴 "报错/不对/403" = bugfix，不能降级。"""),

    Step("1.1", "上下文 + recall", 1, validator=validate_recall,
         instruction="""先读上下文，再跑 recall：
  1. Read ARCHITECTURE.md（Glob **/ARCHITECTURE.md → Read 全文）
  2. 确认 CLAUDE.md 已加载
  3. git log --oneline -15
  4. python3 .claude/hooks/ship_verify_gate.py knowledge_recall --query "<需求一句话>" --top 5

把 recall 命中原文保留，后续拷入 Brief。"""),

    Step("1.2", "并行 Explore", 1, validator=validate_explore,
         done_flags=["--agents"],
         instruction="""在单条消息里并行派 ≥3 个 Explore subagent：

  Agent A — 涉及模块清单（file_path:line、责任、入口、下游）
  Agent B — 现有测试/E2E 覆盖（file_path:line、覆盖范围、缺口）
  Agent C — 相关历史变更（最近 60 天 commit、已修 bug、TODO）

🔴 必须同一条消息发出多个 Agent 调用。主线程禁止亲自 grep/find。

完成后: python3 .claude/tools/fastship_orchestrator.py done --agents <N>"""),

    Step("1.3", "Context Brief", 1, validator=validate_brief,
         instruction="""聚合 subagent 结果 + recall 命中，用 Write 工具写到 .claude/.fastship-brief.md：

  ## Context Brief — {需求}
  ### 涉及模块（agent A）
  - {file_path:line} — {责任} — {入口} — {下游}
  ### 现有测试（agent B）
  - 已覆盖 / 缺口
  ### 历史变更（agent C）
  - 最近 commit / 风险点
  ### 历史教训（recall verbatim）
  {原文拷贝}
  ### 影响
  - 模块清单 / 保护标记 / 回归风险

🔴 必须引用 file_path，不引用 = 凑数。orchestrator 自动检测文件写入并验证章节。"""),

    Step("1.3d", "Bug 诊断", 1, validator=validate_diagnosis, conditional="bugfix",
         instruction="""Bugfix 诊断三步（缺一不可）：

  D1 复现: 实际跑出报错
    python3 .claude/hooks/ship_verify_gate.py bug_diagnosis reproduce --cmd '<命令>'

  D2 根因: 基于 D1 追踪到 file:line + 证据链
    python3 .claude/hooks/ship_verify_gate.py bug_diagnosis root_cause --cause '<根因>'

  D3 验证: 最小改动验证修复方向
    python3 .claude/hooks/ship_verify_gate.py bug_diagnosis fix_verified

🔴 禁止"读代码觉得会报错"，必须实际执行。"""),

    Step("1.4", "写计划", 1, validator=validate_plan,
         instruction="""通过 Skill 工具调用 superpowers 写计划：
  Skill(skill="writing-plans")

计划必须包含 AC 清单 + E2E 验证方案 + 影响范围 + 任务拆分。
🔴 必须通过 Skill 工具调用，不要自己拆步骤。
产物: docs/superpowers/plans/YYYY-MM-DD-{feature}.md
orchestrator 自动检测 plan 文件写入。"""),

    Step("1.5", "Grill", 1, validator=validate_grill,
         instruction="""通过 Skill 工具调用 grill-me 拷问计划：
  Skill(skill="grill-me")

拷问覆盖: Brief 扎实度 / AC 可验证性 / E2E 方案 / 任务粒度 / 保护标记 / 回归风险。
🔴 所有 branch resolved 后才能继续。

完成后用 Write 工具写 grill 摘要到 .claude/.fastship-grill-result.md：

  ## 拷问记录
  1. Q: {问题} → A: {回答} → {resolved/修订了什么}
  2. ...

  ## 修订记录
  - {被修订的 AC/E2E/Plan 条目}

  ## 结论
  - 全部 branch resolved / 打回重做的原因

orchestrator 自动检测文件写入并验证（≥300B + 必须包含 拷问/修订/结论 章节）。"""),

    Step("1.6", "用户确认", 1, validator=validate_user_confirm, done_flags=["--user-confirmed"],
         instruction="""向用户输出 AC + E2E + Plan 摘要，等待明确确认。
🔴 Phase 1 唯一确认关卡。

用户确认后: python3 .claude/tools/fastship_orchestrator.py done --user-confirmed"""),

    Step("2.0", "执行计划", 2, validator=validate_execute,
         instruction="""1. 选择开发方式（worktree / 新分支 / 当前分支）
2. 通过 Skill 工具执行：
   有 subagent: Skill(skill="subagent-driven-development")
   无 subagent: Skill(skill="executing-plans")
🔴 禁止主线程凭直觉写代码。

完成后: python3 .claude/tools/fastship_orchestrator.py done"""),

    Step("3.0", "冒烟测试", 3, validator=validate_smoke,
         instruction="""零 setup 冒烟: 启动服务 → API 请求 → 等处理 → SELECT 验证。
🔴 禁止 DB 写入。失败 → 修，不进 E2E。

完成后: python3 .claude/tools/fastship_orchestrator.py done"""),

    Step("3.1", "项目测试", 3, validator=validate_tests,
         instruction="""运行项目全量测试。hook 自动检测通过。
失败 → 修复后重跑。orchestrator 自动检测 test pass。"""),

    Step("3.2", "E2E Runner", 3, validator=validate_e2e_run,
         instruction="""运行 E2E Runner 采集数据：
  python3 tests/e2e_runner.py -o /tmp/e2e_result.json
🔴 最少 10 轮。Runner 只采集不判断。orchestrator 自动检测。"""),

    Step("3.3", "E2E 报告", 3, validator=validate_e2e_report,
         instruction="""读 /tmp/e2e_result.json，写 E2E 质量检测报告到文件。
报告含: 覆盖度 / 逐轮审查(完整输出) / 总结。
🔴 通过率 < 80% 或 AC 未覆盖 → 不合入。
用 Write 工具保存报告。orchestrator 自动检测文件写入。"""),

    Step("3.4", "E2E Gate", 3, validator=validate_e2e_gate,
         instruction="""运行 Gate 脚本：
  python3 tests/e2e_gate.py --result /tmp/e2e_result.json --min-turns 10
Gate 展示原始数据给用户对照。FAIL → 禁止合入。orchestrator 自动检测。"""),

    Step("3.5", "Loop Record", 3, validator=validate_loop_record,
         instruction="""记录本轮结果：
  通过: python3 .claude/hooks/ship_verify_gate.py loop_record --outcome pass
  失败: 先写 reflection 到 docs/superpowers/plans/<plan>.reflections/loop-N.md
        然后: loop_record --outcome fail --reflection <path>

orchestrator 自动检测，fail 时按 reflection Decision 路由:
  continue → 回 3.1 重试    escalate → 回 1.0    stop → 停止"""),

    Step("3.6", "KNOWLEDGE 闭环", 3, validator=validate_knowledge,
         instruction="""merge 前表态：
  有教训 → 编辑 KNOWLEDGE.md（orchestrator 自动检测）
  无教训 → python3 .claude/hooks/ship_verify_gate.py knowledge_skip --reason "<≥10字>"
"""),
]
```

- [ ] **Step 4: Implement auto-detection functions**

```python
# skills/fastship/orchestrator.py (append)

import re

# ━━━━━━━━━━━━ Auto-Detection ━━━━━━━━━━━━

BRIEF_FILENAME = ".fastship-brief.md"
PLAN_DIR_MARKER = "docs/superpowers/plans/"
E2E_RESULT_PATH = "/tmp/e2e_result.json"


def _normalize(path: str) -> str:
    return (path or "").replace("\\", "/")


def detect_completion_post_bash(current_step: str, data: dict, hook: dict) -> Optional[str]:
    """Check if a bash command completed the current step. Returns step id or None."""
    cmd = data.get("tool_input", {}).get("command", "")
    if not cmd:
        return None

    if current_step == "1.0" and "classify" in cmd and hook.get("request_classified"):
        return "1.0"

    if current_step == "1.1" and "knowledge_recall" in cmd and hook.get("knowledge_recall_done"):
        return "1.1"

    if current_step == "1.3d" and "fix_verified" in cmd and hook.get("bug_diagnosis_done"):
        return "1.3d"

    if current_step == "3.1" and hook.get("test_passed"):
        test_pats = [r'\bpytest\b', r'\bcargo\s+test\b', r'\bnpm\s+test\b', r'\bgo\s+test\b',
                     r'\bnpx\s+(vitest|jest)\b']
        if any(re.search(p, cmd) for p in test_pats):
            return "3.1"

    if current_step == "3.2" and hook.get("e2e_executed"):
        e2e_pats = [r'\be2e', r'\bcurl\s.*localhost', r'\bplaywright\b', r'\bcypress\b']
        if any(re.search(p, cmd, re.IGNORECASE) for p in e2e_pats):
            return "3.2"

    if current_step == "3.4" and re.search(r'\be2e[_-]?gate\b', cmd, re.IGNORECASE):
        return "3.4"

    if current_step == "3.5" and "loop_record" in cmd:
        return "3.5"

    if current_step == "3.6" and "knowledge_skip" in cmd and hook.get("knowledge_acknowledged"):
        return "3.6"

    return None


def detect_completion_post_edit(current_step: str, data: dict) -> Optional[str]:
    """Check if a file write completed the current step. Returns step id or None."""
    file_path = _normalize(data.get("tool_input", {}).get("file_path", ""))
    if not file_path:
        return None

    if current_step == "1.3" and BRIEF_FILENAME in file_path:
        return "1.3"

    if current_step == "1.4" and PLAN_DIR_MARKER in file_path and file_path.endswith(".md"):
        return "1.4"

    if current_step == "1.5" and GRILL_RESULT_FILENAME in file_path:
        return "1.5"

    if current_step == "3.3" and file_path.endswith(".md"):
        # Report file — any markdown file written during 3.3 is candidate
        return "3.3"

    if current_step == "3.6" and os.path.basename(file_path).upper() == "KNOWLEDGE.MD":
        return "3.6"

    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add skills/fastship/orchestrator.py tests/fastship/test_orchestrator.py
git commit -m "feat(fastship): 15 step definitions + auto-detection from hook events"
```

---

## Task 4: Hook Handlers

**Files:**
- Modify: `skills/fastship/orchestrator.py`
- Modify: `tests/fastship/test_orchestrator.py`

This is the key task: making orchestrator the hook entry point.

- [ ] **Step 1: Write failing tests for hook handlers**

```python
# tests/fastship/test_orchestrator.py (append)

class TestHookPreEdit:
    def test_no_session_delegates_to_gate(self, tmp_path):
        """No orchestrator session → fall through to gate."""
        from orchestrator import hook_pre_edit_logic
        result = hook_pre_edit_logic(
            data={"tool_input": {"file_path": "src/main.py"}},
            orch_state=None,
            gate_path="/nonexistent",  # gate won't run
        )
        assert result == 0  # no session, no gate = allow

    def test_phase1_blocks_code_edit(self):
        from orchestrator import hook_pre_edit_logic
        orch = {"current_step": "1.2", "phase": 1, "completed_steps": [],
                "skipped_steps": [], "request_type": "feature", "artifacts": {}}
        result = hook_pre_edit_logic(
            data={"tool_input": {"file_path": "src/main.py"}},
            orch_state=orch,
            gate_path="/nonexistent",
        )
        assert result == 1  # blocked

    def test_phase1_allows_brief_edit(self):
        from orchestrator import hook_pre_edit_logic
        orch = {"current_step": "1.3", "phase": 1, "completed_steps": [],
                "skipped_steps": [], "request_type": "feature", "artifacts": {}}
        result = hook_pre_edit_logic(
            data={"tool_input": {"file_path": ".claude/.fastship-brief.md"}},
            orch_state=orch,
            gate_path="/nonexistent",
        )
        assert result == 0  # allowed

    def test_phase1_allows_plan_edit(self):
        from orchestrator import hook_pre_edit_logic
        orch = {"current_step": "1.4", "phase": 1, "completed_steps": [],
                "skipped_steps": [], "request_type": "feature", "artifacts": {}}
        result = hook_pre_edit_logic(
            data={"tool_input": {"file_path": "docs/superpowers/plans/2026-01-01-x.md"}},
            orch_state=orch,
            gate_path="/nonexistent",
        )
        assert result == 0

    def test_phase2_allows_code_edit(self):
        from orchestrator import hook_pre_edit_logic
        orch = {"current_step": "2.0", "phase": 2, "completed_steps": [],
                "skipped_steps": [], "request_type": "feature", "artifacts": {}}
        result = hook_pre_edit_logic(
            data={"tool_input": {"file_path": "src/main.py"}},
            orch_state=orch,
            gate_path="/nonexistent",
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
                "loop_count": 0, "started_at": "t"}
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestHookPreEdit -v`
Expected: FAIL

- [ ] **Step 3: Implement hook handler logic**

```python
# skills/fastship/orchestrator.py (append)

# ━━━━━━━━━━━━ Code File Detection ━━━━━━━━━━━━

CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".scala",
    ".rb", ".php", ".swift", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".sh", ".bash", ".zsh",
    ".vue", ".svelte", ".html", ".css", ".scss",
}


def _is_code_file(path: str) -> bool:
    p = _normalize(path)
    if "/.claude/" in p or p.startswith(".claude/"):
        return False
    if "/docs/" in p or p.startswith("docs/"):
        return False
    if "/tests/" in p or p.startswith("tests/"):
        return False
    _, ext = os.path.splitext(p)
    return ext.lower() in CODE_EXTENSIONS


def _is_orchestrator_allowed_file(path: str) -> bool:
    """Files always allowed during Phase 1 (brief, plan, reflection, knowledge)."""
    p = _normalize(path)
    if BRIEF_FILENAME in p:
        return True
    if PLAN_DIR_MARKER in p:
        return True
    if os.path.basename(p).upper() == "KNOWLEDGE.MD":
        return True
    if ".reflections/" in p:
        return True
    return False


# ━━━━━━━━━━━━ Orchestrator Advance Logic ━━━━━━━━━━━━

def _get_step_map():
    return {s.id: s for s in STEPS}


def _advance_state(orch: dict) -> dict:
    """Mark current step complete and advance to next (or skip conditional)."""
    step_ids = [s.id for s in STEPS]
    current = orch["current_step"]
    if current not in step_ids:
        return orch

    completed = orch.get("completed_steps", [])
    if current not in completed:
        completed.append(current)
    orch["completed_steps"] = completed

    idx = step_ids.index(current)
    for next_idx in range(idx + 1, len(STEPS)):
        candidate = STEPS[next_idx]
        if candidate.conditional == "bugfix" and orch.get("request_type") != "bugfix":
            skipped = orch.get("skipped_steps", [])
            if candidate.id not in skipped:
                skipped.append(candidate.id)
            orch["skipped_steps"] = skipped
            continue
        orch["current_step"] = candidate.id
        orch["phase"] = candidate.phase
        return orch

    orch["current_step"] = "done"
    return orch


# ━━━━━━━━━━━━ Hook Handlers (Logic) ━━━━━━━━━━━━

def hook_pre_edit_logic(data: dict, orch_state: Optional[dict],
                        gate_path: str) -> int:
    """Pre-edit logic. Returns 0 (allow) or 1 (block)."""
    file_path = data.get("tool_input", {}).get("file_path", "")

    if not orch_state:
        # No session: delegate to gate only
        if os.path.exists(gate_path):
            code, stdout = delegate_to_gate(gate_path, "pre_edit", data)
            if stdout:
                print(stdout, end="")
            return code
        return 0

    # Orchestrator state file protection
    if ".fastship-orchestrator-state.json" in file_path:
        print("🔴 BLOCKED: orchestrator state 由系统管理，禁止手动编辑")
        return 1

    # Phase 1: block code file edits (brief/plan/docs always allowed)
    if orch_state.get("phase", 1) == 1 and _is_code_file(file_path) and not _is_orchestrator_allowed_file(file_path):
        step_map = _get_step_map()
        current = step_map.get(orch_state.get("current_step", ""))
        lines = [
            f"🔴 BLOCKED: Phase 1 进行中，不允许编辑代码文件",
            f"   文件: {file_path}",
            "",
        ]
        if current:
            lines.append(f"📋 当前步骤: {current.id} {current.name}")
            lines.append(f"{'─' * 50}")
            lines.append(current.instruction)
        print("\n".join(lines))
        return 1

    # Phase 2+: delegate to gate for remaining checks
    if os.path.exists(gate_path):
        code, stdout = delegate_to_gate(gate_path, "pre_edit", data)
        if stdout:
            print(stdout, end="")
        return code

    return 0


def hook_pre_bash_logic(data: dict, orch_state: Optional[dict],
                        gate_path: str) -> int:
    """Pre-bash logic. Returns 0 (allow) or 1 (block)."""
    if not orch_state:
        if os.path.exists(gate_path):
            code, stdout = delegate_to_gate(gate_path, "pre_bash", data)
            if stdout:
                print(stdout, end="")
            return code
        return 0

    # Delegate to gate for its own checks (DB write blocking, E2E ordering, etc.)
    if os.path.exists(gate_path):
        code, stdout = delegate_to_gate(gate_path, "pre_bash", data)
        if stdout:
            print(stdout, end="")
        if code != 0:
            return code

    return 0


def hook_post_bash_logic(data: dict, orch_path: str = None,
                         hook_state: dict = None) -> int:
    """Post-bash: delegate to gate first (update state), then auto-detect."""
    orch = load_orch_state(orch_path)
    if not orch or orch.get("current_step") in ("done", "stopped"):
        return 0

    hook = hook_state if hook_state is not None else load_hook_state()
    current = orch.get("current_step")

    detected = detect_completion_post_bash(current, data, hook)
    if detected:
        # Sync request_type from hook
        if detected == "1.0" and hook.get("request_type"):
            orch["request_type"] = hook["request_type"]

        # Handle loop routing for 3.5
        if detected == "3.5":
            outcome = hook.get("last_loop_outcome")
            orch["loop_count"] = hook.get("loop_count", 0)
            if outcome == "pass":
                pass  # fall through to normal advance → 3.6
            else:
                # FAIL: don't auto-route. Pause at 3.5, require manual done --decision.
                orch.setdefault("artifacts", {})["loop_outcome"] = "fail"
                save_orch_state(orch, orch_path)
                print(f"\n📝 Loop {orch['loop_count']} FAIL 已检测。需要手动指定路由：")
                print(f"  python3 .claude/tools/fastship_orchestrator.py done \\")
                print(f"    --outcome fail --decision <continue|escalate|stop>")
                print(f"")
                print(f"  continue  → 回 3.1 重试 (先写 reflection)")
                print(f"  escalate  → 回 1.0 全流程重来 (spec/架构有问题)")
                print(f"  stop      → 停下，输出聚合分析给用户")
                return 0

        orch = _advance_state(orch)
        save_orch_state(orch, orch_path)

        step_map = _get_step_map()
        next_step = step_map.get(orch.get("current_step"))
        if next_step:
            print(f"\n✅ Step {detected} 完成 → 下一步: {next_step.id} {next_step.name}")
        elif orch.get("current_step") == "done":
            print(f"\n✅ Step {detected} 完成 → 🎉 全部完成！可以合入 main。")

    return 0


def hook_post_edit_logic(data: dict, orch_path: str = None) -> int:
    """Post-edit: auto-detect brief/plan/knowledge writes."""
    orch = load_orch_state(orch_path)
    if not orch or orch.get("current_step") in ("done", "stopped"):
        return 0

    current = orch.get("current_step")
    file_path = data.get("tool_input", {}).get("file_path", "")

    detected = detect_completion_post_edit(current, data)
    if detected:
        # For brief (1.3): store path + validate
        if detected == "1.3":
            orch["brief_path"] = file_path
            hook = load_hook_state()
            ok, msg = validate_brief(orch, hook)
            if not ok:
                print(f"⚠️ Brief 写入已检测，但验证未通过: {msg}")
                save_orch_state(orch, orch_path)
                return 0

        # For report (3.3): store path + validate
        if detected == "3.3":
            orch["report_path"] = file_path
            hook = load_hook_state()
            ok, msg = validate_e2e_report(orch, hook)
            if not ok:
                print(f"⚠️ 报告写入已检测，但验证未通过: {msg}")
                save_orch_state(orch, orch_path)
                return 0

        orch = _advance_state(orch)
        save_orch_state(orch, orch_path)

        step_map = _get_step_map()
        next_step = step_map.get(orch.get("current_step"))
        if next_step:
            print(f"\n✅ Step {detected} 完成 → 下一步: {next_step.id} {next_step.name}")
        elif orch.get("current_step") == "done":
            print(f"\n✅ Step {detected} 完成 → 🎉 全部完成！")

    return 0


REWINDABLE_STEPS = {"3.0", "3.1", "3.2", "3.3", "3.4", "3.5"}


def _handle_loop_decision(orch: dict):
    """Route based on decision after loop fail. Called from cmd_done when
    Claude provides --outcome fail --decision <choice>."""
    decision = orch.get("artifacts", {}).get("loop_decision")
    loop_count = orch.get("loop_count", 0)

    if loop_count >= 3:
        orch["current_step"] = "stopped"
        print(f"\n🔴 Loop 上限 ({loop_count}/3) — 流程停止。输出聚合分析给用户。")
        return

    if decision == "continue":
        orch["completed_steps"] = [s for s in orch.get("completed_steps", []) if s not in REWINDABLE_STEPS]
        orch["current_step"] = "3.1"
        orch["phase"] = 3
        for k in ("loop_outcome", "loop_decision"):
            orch.get("artifacts", {}).pop(k, None)
        print(f"\n📝 Loop {loop_count} FAIL → continue → 回到 3.1 重试")
    elif decision == "escalate":
        orch["current_step"] = "1.0"
        orch["phase"] = 1
        orch["completed_steps"] = []
        orch["skipped_steps"] = []
        orch["artifacts"] = {}
        print(f"\n🔴 Loop {loop_count} FAIL → escalate → 回到 1.0 全流程重来")
    elif decision == "stop":
        orch["current_step"] = "stopped"
        print(f"\n🛑 Loop {loop_count} FAIL → stop → 输出聚合分析给用户")
    else:
        print(f"\n❌ 未知 decision: {decision}。必须是 continue|escalate|stop")


# ━━━━━━━━━━━━ Hook Entry Points (stdin) ━━━━━━━━━━━━

def hook_pre_edit():
    data = read_stdin()
    orch = load_orch_state()
    return hook_pre_edit_logic(data, orch, gate_script_path())


def hook_pre_bash():
    data = read_stdin()
    orch = load_orch_state()
    return hook_pre_bash_logic(data, orch, gate_script_path())


def hook_post_bash():
    data = read_stdin()
    # Gate runs first to update hook state
    gp = gate_script_path()
    if os.path.exists(gp):
        code, stdout = delegate_to_gate(gp, "post_bash", data)
        if stdout:
            print(stdout, end="")
    return hook_post_bash_logic(data)


def hook_post_edit():
    data = read_stdin()
    # Gate runs first to update hook state
    gp = gate_script_path()
    if os.path.exists(gp):
        code, stdout = delegate_to_gate(gp, "post_edit", data)
        if stdout:
            print(stdout, end="")
    return hook_post_edit_logic(data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add skills/fastship/orchestrator.py tests/fastship/test_orchestrator.py
git commit -m "feat(fastship): hook handlers — pre_edit blocks Phase 1 code edits, post_* auto-detects"
```

---

## Task 5: CLI Handlers

**Files:**
- Modify: `skills/fastship/orchestrator.py`
- Modify: `tests/fastship/test_orchestrator.py`

- [ ] **Step 1: Write failing tests for CLI**

```python
# tests/fastship/test_orchestrator.py (append)

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
        assert "✅" in output  # completed steps
        assert "👉" in output  # current step

    def test_format_next(self):
        from orchestrator import format_next
        orch = {"current_step": "1.0", "phase": 1}
        output = format_next(orch)
        assert "1.0" in output
        assert "classify" in output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestCLI -v`
Expected: FAIL

- [ ] **Step 3: Implement CLI handlers**

```python
# skills/fastship/orchestrator.py (append)

# ━━━━━━━━━━━━ CLI Arg Parsing ━━━━━━━━━━━━

VALUED_FLAGS = {"--agents", "--brief", "--report", "--outcome", "--decision"}
BOOLEAN_FLAGS = {"--grill-complete", "--user-confirmed"}


def parse_done_args(argv: list) -> dict:
    result = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in VALUED_FLAGS and i + 1 < len(argv):
            result[a] = argv[i + 1]
            i += 2
        elif a in BOOLEAN_FLAGS:
            result[a] = True
            i += 1
        else:
            i += 1
    return result


# ━━━━━━━━━━━━ CLI Formatters ━━━━━━━━━━━━

def format_status(orch: dict) -> str:
    lines = [
        f"🚀 Fastship: {orch.get('requirement', '?')}",
        f"   Phase: {orch.get('phase', '?')} | Step: {orch.get('current_step', '?')} | Loop: {orch.get('loop_count', 0)}/3",
        "",
    ]
    for step in STEPS:
        if step.id in orch.get("completed_steps", []):
            marker = "✅"
        elif step.id in orch.get("skipped_steps", []):
            marker = "⏭️"
        elif step.id == orch.get("current_step"):
            marker = "👉"
        else:
            marker = "⬜"
        lines.append(f"  {marker} {step.id} {step.name}")

    cs = orch.get("current_step")
    if cs == "done":
        lines.append("\n✅ 全部完成")
    elif cs == "stopped":
        lines.append("\n🛑 已停止")
    return "\n".join(lines)


def format_next(orch: dict) -> str:
    step_map = _get_step_map()
    step = step_map.get(orch.get("current_step"))
    if not step:
        cs = orch.get("current_step")
        if cs == "done":
            return "✅ 全部完成。可以合入 main。"
        if cs == "stopped":
            return "🛑 流程停止。输出聚合分析给用户，或 reset 重新开始。"
        return "❓ 未知状态"

    phase_names = {1: "Brainstorm", 2: "Execution", 3: "Verification"}
    return (
        f"📋 Step {step.id}: {step.name}  [{phase_names.get(step.phase, '?')}]\n"
        f"{'─' * 50}\n"
        f"{step.instruction}\n"
        f"{'─' * 50}"
    )


# ━━━━━━━━━━━━ CLI Commands ━━━━━━━━━━━━

def cmd_start(requirement: str) -> int:
    existing = load_orch_state()
    if existing and existing.get("current_step") not in ("done", "stopped", None):
        print(f"⚠️  已有活跃 session: \"{existing.get('requirement')}\"")
        print(f"   当前: {existing.get('current_step')}")
        print(f"   重新开始: python3 .claude/tools/fastship_orchestrator.py reset")
        return 1
    st = empty_orchestrator_state(requirement)
    save_orch_state(st)
    print(f"🚀 Fastship started: \"{requirement}\"\n")
    print(format_next(st))
    return 0


def cmd_next() -> int:
    st = load_orch_state()
    if not st:
        print("❌ 没有活跃 session。先 start。")
        return 1
    print(format_next(st))
    return 0


def cmd_done(argv: list) -> int:
    st = load_orch_state()
    if not st:
        print("❌ 没有活跃 session。")
        return 1
    if st.get("current_step") in ("done", "stopped"):
        print(f"流程已结束 ({st['current_step']})")
        return 0

    step_map = _get_step_map()
    step = step_map.get(st.get("current_step"))
    if not step:
        print("❌ 未知步骤")
        return 1

    args = parse_done_args(argv)

    # Check required flags
    for flag in step.done_flags:
        if flag not in args:
            print(f"❌ Step {step.id} 需要: {flag}")
            return 1

    # Process flags into artifacts
    artifacts = st.get("artifacts", {})
    if "--agents" in args:
        try:
            artifacts["explore_agents"] = int(args["--agents"])
        except ValueError:
            print("❌ --agents 必须是数字")
            return 1
    if "--grill-complete" in args:
        artifacts["grill_complete"] = True
    if "--user-confirmed" in args:
        artifacts["user_confirmed"] = True
    if "--brief" in args:
        st["brief_path"] = args["--brief"]
    if "--report" in args:
        st["report_path"] = args["--report"]
    if "--outcome" in args:
        artifacts["loop_outcome"] = args["--outcome"]
    if "--decision" in args:
        artifacts["loop_decision"] = args["--decision"]
    st["artifacts"] = artifacts

    # Validate
    hook = load_hook_state()
    ok, msg = step.validator(st, hook)
    if not ok:
        print(f"❌ Step {step.id} 验证失败: {msg}")
        save_orch_state(st)
        return 1

    # Special: 3.5 loop fail → route by decision (not normal advance)
    if step.id == "3.5" and artifacts.get("loop_outcome") == "fail":
        if not artifacts.get("loop_decision"):
            print("❌ outcome=fail 必须给 --decision continue|escalate|stop")
            save_orch_state(st)
            return 1
        _handle_loop_decision(st)
        save_orch_state(st)
        next_step = step_map.get(st.get("current_step"))
        if next_step:
            print()
            print(format_next(st))
        return 0

    # Normal advance
    st = _advance_state(st)
    save_orch_state(st)
    next_step = step_map.get(st.get("current_step"))
    print(f"✅ Step {step.id} ({step.name}) 完成")
    if next_step:
        print()
        print(format_next(st))
    elif st.get("current_step") == "done":
        print("\n🎉 全部完成！")
    return 0


def cmd_status() -> int:
    st = load_orch_state()
    if not st:
        print("❌ 没有活跃 session。")
        return 1
    print(format_status(st))
    return 0


def cmd_reset() -> int:
    path = orch_state_path()
    if os.path.exists(path):
        os.remove(path)
    print("✅ Orchestrator state cleared.")
    return 0


# ━━━━━━━━━━━━ Main ━━━━━━━━━━━━

def main():
    if len(sys.argv) < 2:
        print("Usage: fastship_orchestrator.py <command>")
        print()
        print("Hook mode (called by settings.local.json):")
        print("  pre_edit / pre_bash / post_edit / post_bash")
        print()
        print("CLI mode (called by Claude):")
        print("  start \"<需求>\"     开始 session")
        print("  next               当前步骤")
        print("  done [--flags]     完成当前步骤")
        print("  status             全部状态")
        print("  reset              重置")
        sys.exit(1)

    cmd = sys.argv[1]
    handlers = {
        # Hook mode
        "pre_edit": hook_pre_edit,
        "pre_bash": hook_pre_bash,
        "post_edit": hook_post_edit,
        "post_bash": hook_post_bash,
        # CLI mode
        "next": cmd_next,
        "status": cmd_status,
        "reset": cmd_reset,
    }

    if cmd == "start":
        if len(sys.argv) < 3:
            print("Usage: start \"<需求>\"")
            sys.exit(1)
        sys.exit(cmd_start(sys.argv[2]))
    elif cmd == "done":
        sys.exit(cmd_done(sys.argv[2:]))
    elif cmd in handlers:
        sys.exit(handlers[cmd]())
    else:
        print(f"Unknown: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add skills/fastship/orchestrator.py tests/fastship/test_orchestrator.py
git commit -m "feat(fastship): CLI handlers + main entry — dual hook/CLI mode"
```

---

## Task 6: Integration Tests

**Files:**
- Modify: `tests/fastship/test_orchestrator.py`

- [ ] **Step 1: Write integration test — full feature flow via hooks**

```python
# tests/fastship/test_orchestrator.py (append)

class TestIntegrationFullFlow:
    def test_feature_flow_via_hooks(self, tmp_path, monkeypatch):
        """Simulate a complete feature flow through hook auto-detection."""
        from orchestrator import (
            empty_orchestrator_state, save_orch_state, load_orch_state,
            hook_post_bash_logic, hook_post_edit_logic, hook_pre_edit_logic,
            parse_done_args, _advance_state, STEPS
        )

        orch_file = str(tmp_path / "orch.json")
        st = empty_orchestrator_state("add dark mode")
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
        assert result == 1  # blocked

        # 1.1: recall (auto via post_bash)
        hook["knowledge_recall_done"] = True
        hook_post_bash_logic(
            data={"tool_input": {"command": "python3 gate knowledge_recall --query test"}},
            orch_path=orch_file, hook_state=hook)
        st = reload()
        assert st["current_step"] == "1.2"

        # 1.2: explore (manual done)
        st["artifacts"]["explore_agents"] = 3
        save_orch_state(st, orch_file)
        st = _advance_state(st)  # simulate done
        save_orch_state(st, orch_file)
        st = reload()
        assert st["current_step"] == "1.3"

        # 1.3: brief (auto via post_edit)
        brief = tmp_path / "brief.md"
        brief.write_text("## Brief\n### 涉及模块\nx\n### 现有测试\ny\n"
                         "### 历史变更\nz\n### 历史教训\nw\n" + "p " * 100)
        st["brief_path"] = str(brief)
        save_orch_state(st, orch_file)
        hook_post_edit_logic(
            data={"tool_input": {"file_path": str(brief)}},
            orch_path=orch_file)
        st = reload()
        # 1.3d should be skipped (feature)
        assert st["current_step"] == "1.4"
        assert "1.3d" in st["skipped_steps"]

        # 1.4: plan (auto via post_edit)
        hook["plan_ready"] = True
        hook["plan_file"] = "docs/superpowers/plans/2026-05-18-dark.md"
        hook_post_edit_logic(
            data={"tool_input": {"file_path": "docs/superpowers/plans/2026-05-18-dark.md"}},
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
        monkeypatch.setattr("orchestrator._repo_root", lambda: str(tmp_path))
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
        result = hook_pre_edit_logic(
            data={"tool_input": {"file_path": "src/app.py"}},
            orch_state=st, gate_path="/nonexistent")
        assert result == 0  # allowed

        # 2.0: execute (manual done)
        st = _advance_state(st)
        save_orch_state(st, orch_file)
        st = reload()
        assert st["current_step"] == "3.0"

        # 3.0: smoke (manual done)
        st = _advance_state(st)
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

        # 3.6: knowledge (auto)
        hook["knowledge_acknowledged"] = True
        hook_post_edit_logic(
            data={"tool_input": {"file_path": "KNOWLEDGE.md"}},
            orch_path=orch_file)
        st = reload()
        assert st["current_step"] == "done"


    def test_loop_fail_pauses_for_decision(self, tmp_path):
        """Hook detects loop fail → pauses at 3.5, does NOT auto-rewind."""
        from orchestrator import (
            save_orch_state, load_orch_state, hook_post_bash_logic
        )
        orch_file = str(tmp_path / "orch.json")
        st = {
            "requirement": "test", "current_step": "3.5", "phase": 3,
            "completed_steps": ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6",
                                "2.0", "3.0", "3.1", "3.2", "3.3", "3.4"],
            "skipped_steps": ["1.3d"], "request_type": "feature",
            "loop_count": 0, "artifacts": {},
            "brief_path": None, "plan_path": None, "report_path": None,
            "started_at": "t",
        }
        save_orch_state(st, orch_file)

        hook = {"loop_count": 1, "last_loop_outcome": "fail"}
        hook_post_bash_logic(
            data={"tool_input": {"command": "python3 gate loop_record --outcome fail --reflection p"}},
            orch_path=orch_file, hook_state=hook)

        st = load_orch_state(orch_file)
        # Should stay at 3.5, waiting for manual done --decision
        assert st["current_step"] == "3.5"
        assert st["artifacts"]["loop_outcome"] == "fail"

    def test_loop_fail_continue_via_done(self, tmp_path, monkeypatch):
        """Manual done --outcome fail --decision continue → rewinds to 3.1."""
        from orchestrator import _handle_loop_decision
        st = {
            "requirement": "test", "current_step": "3.5", "phase": 3,
            "completed_steps": ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6",
                                "2.0", "3.0", "3.1", "3.2", "3.3", "3.4"],
            "skipped_steps": ["1.3d"], "request_type": "feature",
            "loop_count": 1,
            "artifacts": {"loop_outcome": "fail", "loop_decision": "continue"},
            "brief_path": None, "plan_path": None, "report_path": None,
            "started_at": "t",
        }
        _handle_loop_decision(st)
        assert st["current_step"] == "3.1"
        assert "3.1" not in st["completed_steps"]
        assert "1.0" in st["completed_steps"]

    def test_loop_fail_escalate_via_done(self, tmp_path):
        """Manual done --decision escalate → rewinds to 1.0."""
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

    def test_loop_fail_stop_via_done(self, tmp_path):
        """Manual done --decision stop → enters stopped state."""
        from orchestrator import _handle_loop_decision
        st = {
            "current_step": "3.5", "phase": 3, "loop_count": 2,
            "completed_steps": ["1.0"], "skipped_steps": [],
            "artifacts": {"loop_outcome": "fail", "loop_decision": "stop"},
        }
        _handle_loop_decision(st)
        assert st["current_step"] == "stopped"
```

- [ ] **Step 2: Run integration tests**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestIntegrationFullFlow -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/fastship/test_orchestrator.py
git commit -m "test(fastship): integration tests — full flow + loop-fail-rewind"
```

---

## Task 7: SKILL.md + Installation Updates

**Files:**
- Modify: `skills/fastship/SKILL.md`
- Modify: `skills/fastship/INSTALL.md`
- Modify: `.claude/commands/fastship.md`
- Modify: `.claude/commands/fastship-setup.md`

- [ ] **Step 1: Rewrite SKILL.md**

Replace entire content with:

```markdown
---
name: fastship
description: "Result-driven development skill. Python orchestrator drives every step with hard validation. Works in Claude Code (hook mode) and Codex/other agents (CLI mode)."
---

# /fastship — 结果驱动开发（Python 编排版）

E2E 验证通过为唯一交付标准。Python 状态机驱动每一步，artifact 硬验证，不能跳步。

## 启动

收到需求后立即运行：
  python3 .claude/tools/fastship_orchestrator.py start "<需求>"

## 双模工作方式

### Claude Code（hook 模式 — 最强）

orchestrator 是 hook 入口。每次 Edit/Write/Bash 自动触发：
- **pre_edit**: Phase 1 阻止编辑代码，打印当前步骤
- **post_edit/post_bash**: 自动检测步骤完成，推进下一步

15 步中 10 步自动推进，5 步需手动：
  python3 .claude/tools/fastship_orchestrator.py done [--flags]

### Codex / 其他 Agent（CLI 模式）

无 hook，agent 手动驱动每一步：
  1. `python3 .claude/tools/fastship_orchestrator.py next` → 读当前步骤指令
  2. 执行步骤
  3. `python3 .claude/tools/fastship_orchestrator.py done [--flags]` → 验证 + 推进
  4. 重复

15 步全部需手动 done，但 done 仍做硬性 artifact 验证（文件存在、内容检查）。
Validators 自动检测环境：有 hook state 用 hook state，没有则直接扫文件系统。

## 流程概览

```
Phase 1: Brainstorm (8 步)
  1.0  需求分类         [CC:auto | Codex:done] classify CLI
  1.1  上下文+recall    [CC:auto | Codex:done] knowledge_recall CLI
  1.2  并行 Explore     [CC:done  | Codex:done] done --agents N (≥3)
  1.3  Context Brief    [CC:auto | Codex:done] .fastship-brief.md 验证章节
  1.3d Bug 诊断         [CC:auto | Codex:done] fix_verified (仅 bugfix)
  1.4  写计划           [CC:auto | Codex:done] docs/superpowers/plans/*.md
  1.5  Grill            [CC:auto | Codex:done] .fastship-grill-result.md 验证
  1.6  用户确认         [CC:done  | Codex:done] done --user-confirmed

Phase 2: Execution (1 步)
  2.0  执行计划         [CC:done  | Codex:done]

Phase 3: Verification (6 步)
  3.0  冒烟测试         [CC:done  | Codex:done]
  3.1  项目测试         [CC:auto | Codex:done] test pass
  3.2  E2E Runner       [CC:auto | Codex:done] /tmp/e2e_result.json
  3.3  E2E 报告         [CC:auto | Codex:done] 报告文件 ≥200B
  3.4  E2E Gate         [CC:auto | Codex:done] e2e_gate
  3.5  Loop Record      [CC:auto | Codex:done --outcome pass|fail] loop_record
  3.6  KNOWLEDGE 闭环   [CC:auto | Codex:done] KNOWLEDGE.md
```

## 常用命令

```bash
python3 .claude/tools/fastship_orchestrator.py start "<需求>"  # 启动
python3 .claude/tools/fastship_orchestrator.py next            # 当前步骤
python3 .claude/tools/fastship_orchestrator.py done [--flags]  # 完成 + 验证
python3 .claude/tools/fastship_orchestrator.py status          # 全部状态
python3 .claude/tools/fastship_orchestrator.py reset           # 重置
```

## 核心红线

- Plan 必须走 writing-plans skill（或等价工具产出 plan 文件）
- 执行必须走 executing-plans / subagent-driven-development（或等价）
- Grill 必须对 plan 做结构化拷问
- 主线程禁止亲自 grep/find（改为 1.2 并行 Explore）
- E2E 阶段禁止 DB 写入（Claude Code: gate 拦截; Codex: 自律）
- Loop 上限 3 次
- KNOWLEDGE.md merge 前必须表态
```

- [ ] **Step 2: Update INSTALL.md — add orchestrator, update hook config**

Add after existing Step 2 (hooks):

```markdown
## 2.5 复制 orchestrator

```bash
mkdir -p .claude/tools
cp /path/to/claude-skills/skills/fastship/orchestrator.py .claude/tools/fastship_orchestrator.py
```
```

Replace Step 3 (hook config) — hooks now point to orchestrator:

```markdown
## 3. 配置 hooks

hooks 指向 orchestrator（orchestrator 内部委托 ship_verify_gate）：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit",
        "hooks": [{"type": "command", "command": "python3 .claude/tools/fastship_orchestrator.py pre_edit", "timeout": 10}]
      },
      {
        "matcher": "Write",
        "hooks": [{"type": "command", "command": "python3 .claude/tools/fastship_orchestrator.py pre_edit", "timeout": 10}]
      },
      {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "python3 .claude/tools/fastship_orchestrator.py pre_bash", "timeout": 10}]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "python3 .claude/tools/fastship_orchestrator.py post_bash", "timeout": 10}]
      },
      {
        "matcher": "Edit",
        "hooks": [{"type": "command", "command": "python3 .claude/tools/fastship_orchestrator.py post_edit", "timeout": 10}]
      },
      {
        "matcher": "Write",
        "hooks": [{"type": "command", "command": "python3 .claude/tools/fastship_orchestrator.py post_edit", "timeout": 10}]
      }
    ]
  }
}
```
```

Add to .gitignore section:

```
.claude/.fastship-orchestrator-state.json
.claude/.fastship-brief.md
```

- [ ] **Step 3: Sync `.claude/commands/fastship.md`**

```bash
cp skills/fastship/SKILL.md .claude/commands/fastship.md
```

- [ ] **Step 4: Update `.claude/commands/fastship-setup.md`**

Add orchestrator copy step and update hook config to point to orchestrator.

- [ ] **Step 5: Commit**

```bash
git add skills/fastship/SKILL.md skills/fastship/INSTALL.md \
       .claude/commands/fastship.md .claude/commands/fastship-setup.md
git commit -m "docs(fastship): SKILL.md 852→80 lines, hooks point to orchestrator"
```

---

## Self-Review

1. **Spec coverage:**
   - [x] Claude Code: orchestrator as hook entry point (auto-detection + blocking)
   - [x] Codex/other: orchestrator as CLI tool (manual done + artifact validation)
   - [x] Dual-path validators: hook state → filesystem fallback
   - [x] 11/15 steps auto-detected in Claude Code (including plan signature + grill result file)
   - [x] 4 manual `done` steps: 1.2 (explore), 1.6 (confirm), 2.0 (execute), 3.0 (smoke)
   - [x] Plan signature validation: writing-plans 产出的特征 header 检测，手写 plan 被拒
   - [x] Grill artifact validation: 结构化摘要文件 ≥300B + 必须含 拷问/修订/结论
   - [x] Phase 1 code edit blocking (Claude Code only — hooks enforce)
   - [x] ship_verify_gate.py preserved (subprocess delegation)
   - [x] Loop fail → rewind to 3.1 / escalate to 1.0 / stop
   - [x] Conditional step skip (1.3d for non-bugfix)
   - [x] No-session fallback to gate behavior
   - [x] SKILL.md with dual-mode documentation
   - [x] Installation updates with new hook config
   - [x] Codex fallback tests (monkeypatch filesystem)

2. **Placeholder scan:** No TBDs or TODOs found.

3. **Type consistency:** All validators `(dict, dict) → (bool, str)`. Detection functions return `Optional[str]`. Hook handlers return `int` (exit code). `_read_gate_state_file()` returns `dict`.

4. **Codex compatibility weakness:** Step 3.1 (project tests) has no filesystem artifact — Codex mode relies on gate state file (if agent ran `ship_verify_gate.py post_bash` manually) or soft sequencing. This is the one step where Codex enforcement is weakest. Acceptable tradeoff: test pass detection without hooks is fundamentally hard.

## Version History

| Version | Architecture | Codex |
|---------|-------------|-------|
| v1 | CLI tool (Claude 自愿调用) | ❌ |
| v2 | Hook entry (Claude Code 强制) | ❌ |
| **v2.1** | **Hook entry + CLI dual-mode** | **✅ 双路径验证** |

## Key Differences

| | v1 | v2 | v2.1 (本 plan) |
|---|---|---|---|
| orchestrator 角色 | 可选 CLI | hook 入口 | **hook 入口 + CLI 双模** |
| 步骤推进 (CC) | 18 次手动 | 10 自动 + 5 手动 | 11 自动 + 4 手动 |
| 步骤推进 (Codex) | N/A | N/A | **15 次手动 + artifact 验证** |
| Validators | hook state only | hook state only | **hook state → fs fallback** |
| Phase 1 阻断 | plan_ready check | phase check | phase check (CC) / 无 (Codex) |
| 绕过可能 (CC) | 可绕过 | 无法绕过 | 无法绕过 |
| 绕过可能 (Codex) | N/A | N/A | **可绕过但 done 做硬验证** |
