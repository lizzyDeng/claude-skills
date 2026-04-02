#!/usr/bin/env python3
"""
ship_verify_gate.py — /ship E2E 验证硬性 Gate（通用版）

保证 /ship 阶段 3 的测试流程不能被跳过。
仅在 ship/* 或 worktree-ship-* 分支上激活，其他分支无任何影响。

动作:
  post_bash  — PostToolUse: Bash → 检测测试/E2E 执行，写入验证 stamp
  pre_bash   — PreToolUse: Bash → 检测 git merge，stamp 不全则阻断（exit 1）
  pre_edit   — PreToolUse: Edit/Write → 禁止 LLM 篡改验证状态文件
  status     — 查看当前验证状态

状态文件: {repo_root}/.claude/.ship-verify-state.json

技术栈自动检测:
  - Rust (Cargo.toml)     → cargo test
  - Node (package.json)   → npm test / npx vitest / npx jest
  - Python (pyproject.toml / setup.py / requirements.txt) → pytest / python -m pytest
  - Go (go.mod)           → go test
"""

import sys
import os
import json
import subprocess
import re
from datetime import datetime


# ---------- 基础工具 ----------

def get_repo_root():
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def get_current_branch():
    try:
        r = subprocess.run(["git", "branch", "--show-current"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def is_ship_branch(branch):
    """ship/ 分支 或 EnterWorktree 创建的 worktree-ship-* 分支"""
    return bool(branch and (
        branch.startswith("ship/") or
        branch.startswith("worktree-ship-")
    ))


def state_path():
    root = get_repo_root()
    return os.path.join(root, ".claude", ".ship-verify-state.json") if root else None


def empty_state(branch=None):
    return {
        "test_passed": False,
        "test_ts": None,
        "test_tool": None,
        "e2e_executed": False,
        "e2e_ts": None,
        "branch": branch,
    }


def load_state():
    p = state_path()
    if p and os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return empty_state()


def save_state(st):
    p = state_path()
    if not p:
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(st, f, indent=2)


def read_stdin():
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def extract_output(data):
    """从 hook data 中提取命令 stdout"""
    resp = data.get("tool_response", {})
    if isinstance(resp, dict):
        stdout = resp.get("stdout", "")
        if isinstance(stdout, str):
            return stdout
    for key in ("tool_output", "tool_result", "output", "stdout"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict) and isinstance(val.get("stdout"), str):
            return val["stdout"]
    return ""


def ensure_branch_state(st, branch):
    """分支变化时自动重置状态"""
    if st.get("branch") != branch:
        return empty_state(branch)
    return st


# ---------- 技术栈检测 ----------

def detect_stack():
    """检测项目技术栈，返回列表"""
    root = get_repo_root()
    if not root:
        return []
    stacks = []
    markers = {
        "rust": "Cargo.toml",
        "node": "package.json",
        "python": ["pyproject.toml", "setup.py", "requirements.txt"],
        "go": "go.mod",
    }
    for stack, files in markers.items():
        if isinstance(files, str):
            files = [files]
        for f in files:
            if os.path.exists(os.path.join(root, f)):
                stacks.append(stack)
                break
    return stacks


# ---------- 命令模式检测（多技术栈） ----------

TEST_PATTERNS = {
    "rust": (r'\bcargo\s+test\b', r'test result: ok'),
    "node": (r'\b(npm\s+test|npx\s+(vitest|jest)|yarn\s+test|pnpm\s+test)\b', r'(Tests?\s+\d+\s+passed|passing|✓|\bPASS\b)'),
    "python": (r'\b(pytest|python3?\s+-m\s+pytest|python3?\s+-m\s+unittest)\b', r'(passed|OK\b|\d+ passed)'),
    "go": (r'\bgo\s+test\b', r'\bok\b'),
}


def is_test_cmd(cmd):
    """检测是否是测试命令，返回 (bool, stack_name)"""
    if not cmd:
        return False, None
    for stack, (pattern, _) in TEST_PATTERNS.items():
        if re.search(pattern, cmd):
            return True, stack
    return False, None


def test_passed(output, stack):
    """检测测试是否通过"""
    if not output or not stack:
        return False
    _, pass_pattern = TEST_PATTERNS.get(stack, (None, None))
    if not pass_pattern:
        return False
    return bool(re.search(pass_pattern, output))


def is_e2e_cmd(cmd):
    """检测 E2E 验证命令"""
    if not cmd:
        return False
    pats = [
        r'\bagent-browser\b',
        r'\bcurl\s.*localhost',
        r'\bplaywright\b',
        r'\bcypress\b',
        r'\bpuppeteer\b',
        r'\bselenium\b',
        r'\bpython3?\s.*e2e',
        r'\bpytest\b.*e2e',
        r'\bnpm\s+run\s+.*e2e',
        r'\be2e[_-]?(test|run|check)\b',
    ]
    return any(re.search(p, cmd, re.IGNORECASE) for p in pats)


def is_merge_cmd(cmd):
    """检测合入/切回 main 的命令"""
    if not cmd:
        return False
    pats = [
        r'\bgit\s+merge\b',
        r'\bgit\s+checkout\s+(main|master)\b',
        r'\bgit\s+switch\s+(main|master)\b',
    ]
    return any(re.search(p, cmd) for p in pats)


# ---------- Gate 动作 ----------

def gate_pre_edit():
    """PreToolUse: Edit/Write — 禁止 LLM 直接修改 state file"""
    data = read_stdin()
    file_path = data.get("tool_input", {}).get("file_path", "")
    if file_path and ".ship-verify-state.json" in file_path:
        print("🔴 BLOCKED: .ship-verify-state.json 由 hook 自动管理，禁止手动编辑")
        return 1
    return 0


def gate_post_bash():
    """PostToolUse: Bash — 检测测试/E2E 执行结果，写入 stamp"""
    branch = get_current_branch()
    if not is_ship_branch(branch):
        return 0

    data = read_stdin()
    cmd = data.get("tool_input", {}).get("command", "")
    output = extract_output(data)

    st = ensure_branch_state(load_state(), branch)
    now = datetime.now().isoformat()
    changed = False

    # 检测项目测试
    is_test, stack = is_test_cmd(cmd)
    if is_test:
        if test_passed(output, stack):
            st["test_passed"] = True
            st["test_ts"] = now
            st["test_tool"] = stack
            changed = True
            print(f"✅ Ship Gate: {stack} test 通过，已记录")
        else:
            print(f"⚠️ Ship Gate: {stack} test 已执行，但未检测到通过标志")

    # 检测 E2E 验证
    if is_e2e_cmd(cmd):
        st["e2e_executed"] = True
        st["e2e_ts"] = now
        changed = True
        print("✅ Ship Gate: E2E 验证已执行，已记录")

    if changed:
        save_state(st)

    # Proactive reminder
    st = load_state()
    missing = []
    if not st.get("test_passed"):
        missing.append("项目测试")
    if not st.get("e2e_executed"):
        missing.append("E2E 验证")
    if missing:
        print(f"⚠️ Ship Gate 提醒: 合入前仍需完成 → {', '.join(missing)}")

    return 0


def gate_pre_bash():
    """PreToolUse: Bash — 合入 main 前检查验证 stamp，不全则阻断"""
    branch = get_current_branch()
    if not is_ship_branch(branch):
        return 0

    data = read_stdin()
    cmd = data.get("tool_input", {}).get("command", "")
    if not is_merge_cmd(cmd):
        return 0

    st = ensure_branch_state(load_state(), branch)

    blocks = []
    if not st.get("test_passed"):
        blocks.append("项目测试未通过（/ship 阶段3 Step 1）")
    if not st.get("e2e_executed"):
        blocks.append("E2E 验证未执行（/ship 阶段3 Step 2）")

    if blocks:
        lines = [
            "🔴 BLOCKED: /ship 验证未完成，禁止合入 main",
        ]
        for b in blocks:
            lines.append(f"   ❌ {b}")
        lines.append("")
        lines.append("请先完成 /ship 阶段 3 验证流程：")
        lines.append("   1. 项目测试（全量）")
        lines.append("   2. E2E 验证（按阶段 1 定义的方案）")
        lines.append("   3. 回归检查")
        print("\n".join(lines))
        return 1

    print("✅ Ship Gate: 验证已完成，允许合入")
    return 0


def gate_status():
    """打印当前验证状态"""
    branch = get_current_branch()
    st = load_state()
    stacks = detect_stack()
    print(f"Branch:     {branch} ({'ship' if is_ship_branch(branch) else 'non-ship'})")
    print(f"State for:  {st.get('branch', '-')}")
    print(f"Stack:      {', '.join(stacks) if stacks else 'unknown'}")
    t = "✅" if st.get("test_passed") else "❌"
    e = "✅" if st.get("e2e_executed") else "❌"
    tool = st.get("test_tool", "-")
    print(f"Test:       {t}  ({tool}) ({st.get('test_ts', '-')})")
    print(f"E2E:        {e}  ({st.get('e2e_ts', '-')})")
    return 0


# ---------- 入口 ----------

def main():
    if len(sys.argv) < 2:
        print("Usage: ship_verify_gate.py <post_bash|pre_bash|pre_edit|status>")
        sys.exit(1)

    handlers = {
        "post_bash": gate_post_bash,
        "pre_bash": gate_pre_bash,
        "pre_edit": gate_pre_edit,
        "status": gate_status,
    }
    handler = handlers.get(sys.argv[1])
    if handler:
        sys.exit(handler())
    else:
        print(f"Unknown action: {sys.argv[1]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
