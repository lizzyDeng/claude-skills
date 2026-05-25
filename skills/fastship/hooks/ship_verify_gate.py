#!/usr/bin/env python3
"""
ship_verify_gate.py — E2E 验证硬性 Gate（通用版）

保证任何需求的测试 + E2E 验证流程不能被跳过。
所有分支均生效（包括 main），不再局限于 ship/* 分支。

动作:
  post_bash       — PostToolUse: Bash → 检测测试/E2E 执行，更新 loop 计数器
  post_edit       — PostToolUse: Edit/Write → 检测 Plan/KNOWLEDGE.md 写入
  pre_bash        — PreToolUse: Bash → 六层自动 Gate:
                    Gate 0: E2E 阶段禁止直接 DB 写入
                    Gate 1: E2E 命令 → 必须先跑通项目测试
                    Gate 2: E2E Gate 脚本 → 必须测试 + E2E Runner 都完成
                    Gate 3: merge/push → 必须全部验证完成
                    Gate 4: merge/push → 必须 KNOWLEDGE.md 已更新或显式声明跳过
                    Gate 5: 重跑 E2E → 上一轮必须已 loop_record，失败时必须有 reflection
  pre_edit        — PreToolUse: Edit/Write → 双层自动 Gate:
                    Gate A: 禁止 LLM 篡改验证状态文件
                    Gate B: 编辑代码前必须有 plan 文件（Phase 2 前置条件）
  status          — 查看当前验证状态
  reset           — 手动重置状态（新需求开始时）
  plan_bypass     — 在当前分支放行 Plan Gate（非 fastship 流程下的兜底）
  knowledge_skip  — 显式声明本次无新教训需要写入 KNOWLEDGE.md（必须给 --reason）
  knowledge_recall — 跨 session 学习：检索所有 KNOWLEDGE.md，返回与 --query 相关的 top-N
                     条目原文。必须在 1.1 执行（编辑代码前 Gate B 强制）。
  loop_record     — 记录本轮 loop 结果：
                    sequential: --outcome pass | fail [--reflection <path>]
                    parallel:   --outcome pass | fail --reflection-dir <dir>
                                                       [--winner <key>]   (pass 必填)
  classify        — 1.0 需求分类（强制第一步，bugfix 自动激活诊断 Gate）：
                    --type bugfix|feature|refactor|optimize
  bug_diagnosis   — Bug 诊断 Gate（1.1d，Bugfix 场景强制）：
                    mark_bugfix                  — 标记为 Bugfix（激活诊断 Gate）
                    reproduce --cmd "..."        — D1 完成，记录实际复现命令
                    root_cause --cause "..."     — D2 完成，记录根因（须基于 D1 输出）
                    fix_verified                 — D3 完成，修复假设已验证
                    skip --reason "..."          — 非 Bugfix，跳过诊断 Gate

状态文件: {git_common_dir}/fastship/gate.json

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

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TOOLS_DIR = os.path.join(SOURCE_DIR, "tools")
for path in (TOOLS_DIR, SOURCE_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import fastship_state


# ---------- 基础工具 ----------

def get_repo_root():
    return fastship_state.repo_root()


def get_current_branch():
    return fastship_state.current_branch()


def is_main_branch(branch):
    """检测是否在 main/master 分支"""
    return branch in ("main", "master")


def state_path():
    return fastship_state.gate_state_path()


LOOP_LIMIT = 3


def empty_state(branch=None):
    return {
        "test_passed": False,
        "test_ts": None,
        "test_tool": None,
        "e2e_executed": False,
        "e2e_ts": None,
        "e2e_result_hash": None,
        "e2e_result_turns": None,
        "e2e_runner_cmd": None,
        "e2e_gate_passed": False,
        "e2e_gate_ts": None,
        "plan_ready": False,
        "plan_file": None,
        "plan_ts": None,
        "plan_bypass": False,
        "knowledge_acknowledged": False,
        "knowledge_file": None,
        "knowledge_ts": None,
        "knowledge_skip_reason": None,
        # 跨 session 学习：1.1 阶段必须跑过 knowledge_recall
        "knowledge_recall_done": False,
        "knowledge_recall_query": None,
        "knowledge_recall_count": None,
        "knowledge_recall_ts": None,
        # 1.0 需求分类（强制第一步）
        "request_classified": False,
        "request_type": None,                  # bugfix | feature | refactor | optimize
        "request_classified_ts": None,
        # Bug 诊断 Gate（1.1d，Bugfix 场景强制，classify --type bugfix 自动激活）
        "bug_diagnosis_done": False,
        "bug_diagnosis_ts": None,
        "bug_diagnosis_reproduce": None,       # D1 复现命令
        "bug_diagnosis_root_cause": None,      # D2 根因一句话
        "bug_diagnosis_fix_verified": False,   # D3 修复假设验证
        "bug_is_bugfix": False,                # 当前需求是否为 Bugfix
        # Reflection in Loop
        "loop_count": 0,                       # 已记录的 loop 数（loop_record 调用次数）
        "e2e_runs_since_last_record": 0,       # 自上次 loop_record 后跑了几次 E2E（>=1 时再跑会被拦）
        "last_loop_outcome": None,             # "pass" | "fail" | None
        "last_loop_reflection": None,          # 失败时 reflection 文件绝对/相对路径
        "loop_history": [],                    # [{"loop": N, "outcome": "...", "reflection": "...", "ts": "..."}]
        "branch": branch,
    }


def load_state():
    fastship_state.migrate_legacy_state("gate")
    p = state_path()
    if p and os.path.exists(p):
        try:
            data = fastship_state.load_json(p)
            if data is not None:
                return data
        except Exception:
            pass
    return empty_state()


def save_state(st):
    p = state_path()
    if not p:
        return
    fastship_state.save_json(p, st)


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


def extract_exit_code(data):
    """从 hook data 中提取 bash exit code，找不到返回 None"""
    resp = data.get("tool_response", {})
    if isinstance(resp, dict):
        for key in ("exitCode", "exit_code", "returnCode", "return_code"):
            val = resp.get(key)
            if val is not None:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    pass
    for key in ("exitCode", "exit_code"):
        val = data.get(key)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
    return None


E2E_RESULT_PATH = "/tmp/e2e_result.json"

E2E_FAILURE_PATTERNS = [
    r'Connection\s+refused',
    r'ECONNREFUSED',
    r'ERR_CONNECTION_REFUSED',
    r'Traceback \(most recent call last\)',
    r'Error:\s+.*(?:crash|fatal|segfault)',
    r'(?<![0 ])\bFAILED\b',
    r'TimeoutError',
    r'Cannot connect',
]


def e2e_succeeded(data):
    """判断 E2E 命令是否真正成功。三层检查：
    1. exit code 非 0 → 失败；exit code == 0 → 信任，跳过模式匹配
    2. /tmp/e2e_result.json 有 pass/fail → 以此为准
    3. 输出包含明显失败模式 → 失败（仅当 exit code 未知时）
    exit code 未知 + 无 result file + 无失败模式 → 保守认为成功（向后兼容）
    """
    exit_code = extract_exit_code(data)
    if exit_code is not None and exit_code != 0:
        return False, f"exit code={exit_code}"

    if os.path.exists(E2E_RESULT_PATH):
        try:
            with open(E2E_RESULT_PATH) as f:
                result = json.load(f)
            status = result.get("status", result.get("result", "")).lower()
            if status in ("pass", "passed", "ok", "success"):
                return True, "e2e_result.json: pass"
            if status in ("fail", "failed", "error"):
                return False, f"e2e_result.json: {status}"
        except Exception:
            pass

    if exit_code == 0:
        return True, "exit code=0"

    output = extract_output(data)
    for pat in E2E_FAILURE_PATTERNS:
        if re.search(pat, output, re.IGNORECASE):
            return False, f"output matched failure pattern: {pat}"

    return True, "no failure signals detected"


def ensure_branch_state(st, branch):
    """Keep state intact across branch changes; callers decide whether to block."""
    return st


def branch_mismatch(st, branch=None):
    return fastship_state.branch_mismatch(st, branch)


def print_branch_mismatch(st):
    print("🔴 Fastship gate paused because the branch changed.")
    print("\n".join(fastship_state.branch_mismatch_lines(st, "Fastship gate")))


def require_branch_match(st, branch=None):
    if branch_mismatch(st, branch):
        print_branch_mismatch(st)
        return False
    return True


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


def is_e2e_gate_cmd(cmd):
    """检测 E2E Gate 脚本命令"""
    if not cmd:
        return False
    return bool(re.search(r'\be2e[_-]?gate\b', cmd, re.IGNORECASE))


E2E_RUNNER_STRICT_PATTERNS = [
    r'\bpython3?\s+.*e2e[_-]?runner\b',
    r'\bplaywright\s+test\b',
    r'\bcypress\s+run\b',
    r'\bnpm\s+run\s+.*e2e\b',
]


def is_strict_e2e_runner(cmd):
    """Strict pattern: only matches actual E2E runner scripts, not arbitrary commands containing 'e2e'."""
    if not cmd:
        return False
    return any(re.search(p, cmd, re.IGNORECASE) for p in E2E_RUNNER_STRICT_PATTERNS)


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


PLAN_DIR_MARKER = "docs/superpowers/plans/"

# 必须先有 plan 才能编辑的代码文件扩展名（明显的实现代码）
# 配置类（.json/.yml/.toml）和文档类（.md）不在此列，允许自由编辑
CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".scala",
    ".rb", ".php", ".swift", ".m", ".mm",
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx",
    ".cs", ".vb",
    ".sh", ".bash", ".zsh",
    ".vue", ".svelte",
    ".html", ".css", ".scss", ".less",
}


def normalize_path(path):
    """统一分隔符，方便匹配"""
    return (path or "").replace("\\", "/")


def is_plan_file(path):
    """判断是否是 superpowers writing-plans 产出的计划文件"""
    p = normalize_path(path)
    return PLAN_DIR_MARKER in p and p.endswith(".md")


def is_knowledge_file(path):
    """判断是否是项目 KNOWLEDGE.md（任意层级，文件名严格匹配）"""
    p = normalize_path(path)
    if not p:
        return False
    return os.path.basename(p).upper() == "KNOWLEDGE.MD"


def is_code_file(path):
    """判断是否是需要 plan 才能编辑的代码文件"""
    p = normalize_path(path)
    if not p:
        return False
    # 明确放行：.claude/ 配置目录、docs/ 文档目录
    if "/.claude/" in p or p.startswith(".claude/"):
        return False
    if p.startswith("docs/") or "/docs/" in p:
        return False
    _, ext = os.path.splitext(p)
    return ext.lower() in CODE_EXTENSIONS


def is_db_write_cmd(cmd):
    """检测直接操作数据库的写入命令（INSERT/UPDATE/DELETE via psql/docker exec psql）。
    用于 E2E 阶段拦截——防止 LLM 通过手动构造数据来 fake 测试结果。
    SELECT 不拦截（用于验证副作用）。"""
    if not cmd:
        return False
    # Must involve psql or similar DB CLI
    db_cli = re.search(r'\bpsql\b|\bsqlite3\b|\bmongo\b|\bmysql\b', cmd, re.IGNORECASE)
    if not db_cli:
        return False
    # Must contain write operations
    write_ops = re.search(
        r'\bINSERT\s+INTO\b|\bUPDATE\s+\w+\s+SET\b|\bDELETE\s+FROM\b|\bTRUNCATE\b|\bDROP\b|\bALTER\b',
        cmd, re.IGNORECASE
    )
    return bool(write_ops)


FASTSHIP_STATE_PATTERNS = [
    "fastship/gate.json",
    "fastship/orchestrator.json",
    ".ship-verify-state.json",
    ".fastship-orchestrator-state.json",
]


def is_fastship_state_file(path):
    """Check if a path points to any fastship state file (gate or orchestrator)."""
    p = normalize_path(path)
    return any(pat in p for pat in FASTSHIP_STATE_PATTERNS)


def is_state_file_write_cmd(cmd):
    """Detect bash commands that write to fastship state files.
    Allows reads (cat without redirect) and gate script invocations without redirect."""
    if not cmd:
        return False
    # Check for redirect/write patterns targeting state files FIRST
    # (even gate scripts with redirect are blocked: `gate.py status > gate.json`)
    for pat in FASTSHIP_STATE_PATTERNS:
        if pat not in cmd:
            continue
        # Redirect to state file = always blocked, no exceptions
        if re.search(r'>\s*.*' + re.escape(pat), cmd):
            return True
    # Gate scripts WITHOUT redirect are allowed (they write via Python open())
    if "ship_verify_gate.py" in cmd or "fastship_orchestrator.py" in cmd:
        return False
    # Other commands writing to state files
    for pat in FASTSHIP_STATE_PATTERNS:
        if pat not in cmd:
            continue
        if re.search(r'\b(echo|printf|python3?|tee|cp|mv)\b.*' + re.escape(pat), cmd):
            return True
    return False


# ---------- Gate 动作 ----------

def gate_pre_edit():
    """PreToolUse: Edit/Write — 双层 Gate:
       Gate A: 禁止 LLM 直接修改 state file
       Gate B: 编辑代码前必须有 plan 文件（Phase 2 前置条件）
    """
    data = read_stdin()
    file_path = data.get("tool_input", {}).get("file_path", "")

    # --- Gate A: 保护 state file ---
    if file_path and is_fastship_state_file(file_path):
        print(f"🔴 BLOCKED: fastship state file 由 hook 自动管理，禁止手动编辑 ({file_path})")
        return 1

    # --- Gate B: 代码编辑前必须有 plan + 已 knowledge_recall ---
    if is_code_file(file_path):
        branch = get_current_branch()
        st = ensure_branch_state(load_state(), branch)
        if not require_branch_match(st, branch):
            return 1

        # 环境变量兜底（便于 CI / 临时绕过）
        if os.environ.get("FASTSHIP_SKIP_PLAN_GATE") == "1":
            return 0
        if st.get("plan_bypass"):
            return 0

        problems = []
        if not st.get("request_classified"):
            problems.append("request_classified=false（1.0 需求分类未完成）")
        if not st.get("plan_ready"):
            problems.append("plan_ready=false（未检测到 plan 文件）")
        if not st.get("knowledge_recall_done"):
            problems.append("knowledge_recall_done=false（1.1 阶段未跑跨 session 检索）")
        if st.get("bug_is_bugfix") and not st.get("bug_diagnosis_done"):
            diag_missing = []
            if not st.get("bug_diagnosis_reproduce"):
                diag_missing.append("D1 复现")
            if not st.get("bug_diagnosis_root_cause"):
                diag_missing.append("D2 根因")
            if not st.get("bug_diagnosis_fix_verified"):
                diag_missing.append("D3 修复验证")
            problems.append(f"bug_diagnosis_done=false（Bugfix 未完成诊断：{' → '.join(diag_missing)}）")

        if not problems:
            return 0

        lines = [
            "🔴 BLOCKED: 进入阶段 2 前置条件未满足",
            "",
            f"   目标文件：{file_path}",
        ]
        for p in problems:
            lines.append(f"   ❌ {p}")
        lines.append("")
        if not st.get("request_classified"):
            lines += [
                "   先分类需求类型（1.0 强制第一步）：",
                '     python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" classify \\',
                "       --type bugfix|feature|refactor|optimize",
                "",
            ]
        if not st.get("knowledge_recall_done"):
            lines += [
                "   先跑 knowledge_recall（1.1 阶段必做，跨 session 学习）：",
                '     python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" knowledge_recall \\',
                "       --query \"<需求一句话>\"",
                "",
            ]
        if st.get("bug_is_bugfix") and not st.get("bug_diagnosis_done"):
            lines += [
                "   🐛 Bugfix 诊断 Gate 未完成，按顺序执行：",
                "     D1 复现：跑测试/curl 拿到实际报错，然后：",
                '       python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" bug_diagnosis reproduce \\',
                "         --cmd '<你实际执行的复现命令>'",
                "     D2 根因：基于 D1 输出追踪到 file:line，然后：",
                '       python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" bug_diagnosis root_cause \\',
                "         --cause '<根因一句话>'",
                "     D3 验证：最小改动验证修复方向，然后：",
                '       python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" bug_diagnosis fix_verified',
                "",
            ]
        if not st.get("plan_ready"):
            lines += [
                "   再通过 Skill 工具调用 superpowers 的 writing-plans：",
                "     Skill(skill=\"writing-plans\")",
                "   把计划落盘到：",
                f"     {PLAN_DIR_MARKER}YYYY-MM-DD-<feature>.md",
                "",
            ]
        lines += [
            "   全部就绪后 Gate 自动放行。",
            "",
            "   非 /fastship 流程可临时放行 Plan Gate：",
            '     python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" plan_bypass',
        ]
        print("\n".join(lines))
        return 1

    return 0


def gate_post_edit():
    """PostToolUse: Edit/Write — 检测 plan 文件 / KNOWLEDGE.md 写入"""
    data = read_stdin()
    file_path = data.get("tool_input", {}).get("file_path", "")

    branch = get_current_branch()
    st = ensure_branch_state(load_state(), branch)
    now = datetime.now().isoformat()
    changed = False

    if is_plan_file(file_path):
        st["plan_ready"] = True
        st["plan_file"] = normalize_path(file_path)
        st["plan_ts"] = now
        changed = True
        print(f"✅ Gate: 检测到 plan 文件已写入，plan_ready=true ({file_path})")

    if is_knowledge_file(file_path):
        st["knowledge_acknowledged"] = True
        st["knowledge_file"] = normalize_path(file_path)
        st["knowledge_ts"] = now
        st["knowledge_skip_reason"] = None
        changed = True
        print(f"✅ Gate: 检测到 KNOWLEDGE.md 更新，knowledge_acknowledged=true ({file_path})")

    if changed:
        if not require_branch_match(st, branch):
            return 0
        save_state(st)
    return 0


def gate_plan_bypass():
    """手动放行 Plan Gate（当前分支生效，用于非 fastship 场景）"""
    branch = get_current_branch()
    st = ensure_branch_state(load_state(), branch)
    if not require_branch_match(st, branch):
        return 1
    st["plan_bypass"] = True
    save_state(st)
    print(f"⚠️ Gate: 已为当前分支 ({branch}) 放行 Plan Gate")
    print("   — 如果你原本在跑 /fastship，请立即 reset 并补写计划")
    return 0


def _resolve_path(path):
    """相对路径相对 repo_root 解析；返回存在的路径或 None"""
    if not path:
        return None
    candidates = [path]
    if not os.path.isabs(path):
        repo_root = get_repo_root() or os.getcwd()
        candidates.append(os.path.join(repo_root, path))
    return next((p for p in candidates if os.path.exists(p)), None)


def _validate_reflection_file(path):
    """校验 reflection markdown 文件存在 + 体积 ≥200B；返回 (resolved_path, error_msg|None)"""
    resolved = _resolve_path(path)
    if not resolved:
        return None, f"🔴 reflection 文件不存在：{path}"
    try:
        size = os.path.getsize(resolved)
    except OSError:
        size = 0
    if size < 200:
        return None, f"🔴 reflection 文件太小（{size}B < 200B）：{path}"
    return resolved, None


def gate_loop_record():
    """记录本轮 loop 结果：
       sequential：
         --outcome pass                        → 直接通过
         --outcome fail --reflection <path>    → 一份反思
       parallel（一次并行探索 = 1 次 loop_record，不是 N 次）：
         --outcome pass --reflection-dir <dir> --winner <key>
                                               → ≥2 份反思，winner 必填
         --outcome fail --reflection-dir <dir> → ≥2 份反思（全失败）
    """
    args = sys.argv[2:]
    outcome = None
    reflection = None
    reflection_dir = None
    winner = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--outcome",) and i + 1 < len(args):
            outcome = args[i + 1]; i += 2; continue
        if a.startswith("--outcome="):
            outcome = a.split("=", 1)[1]; i += 1; continue
        if a == "--reflection" and i + 1 < len(args):
            reflection = args[i + 1]; i += 2; continue
        if a.startswith("--reflection="):
            reflection = a.split("=", 1)[1]; i += 1; continue
        if a == "--reflection-dir" and i + 1 < len(args):
            reflection_dir = args[i + 1]; i += 2; continue
        if a.startswith("--reflection-dir="):
            reflection_dir = a.split("=", 1)[1]; i += 1; continue
        if a == "--winner" and i + 1 < len(args):
            winner = args[i + 1]; i += 2; continue
        if a.startswith("--winner="):
            winner = a.split("=", 1)[1]; i += 1; continue
        i += 1

    if outcome not in ("pass", "fail"):
        print("🔴 必须给 --outcome pass | fail")
        return 1
    if reflection and reflection_dir:
        print("🔴 --reflection 与 --reflection-dir 互斥（前者 sequential，后者 parallel）")
        return 1

    branch = get_current_branch()
    st = ensure_branch_state(load_state(), branch)
    if not require_branch_match(st, branch):
        return 1

    if not st.get("e2e_executed") or st.get("e2e_runs_since_last_record", 0) < 1:
        print("🔴 当前没有未记录的 E2E 运行可供 loop_record（先跑 E2E Runner）")
        return 1

    mode = "sequential"
    parallel_files = []
    parallel_winner_path = None

    if reflection_dir:
        mode = "parallel"
        resolved_dir = _resolve_path(reflection_dir)
        if not resolved_dir or not os.path.isdir(resolved_dir):
            print(f"🔴 reflection-dir 不是已存在的目录：{reflection_dir}")
            return 1
        md_files = sorted(
            os.path.join(resolved_dir, f)
            for f in os.listdir(resolved_dir)
            if f.endswith(".md")
        )
        if len(md_files) < 2:
            print(f"🔴 parallel 模式需要 ≥2 份反思 .md，目录里只有 {len(md_files)} 份")
            return 1
        for f in md_files:
            _, err = _validate_reflection_file(f)
            if err:
                print(err)
                return 1
        parallel_files = [normalize_path(f) for f in md_files]

        if outcome == "pass":
            if not winner:
                print("🔴 parallel 模式 outcome=pass 必须给 --winner <key>")
                print("   key = reflection 文件名去掉 .md，例：--winner hypothesis-a")
                return 1
            winner_basenames = [os.path.splitext(os.path.basename(f))[0] for f in md_files]
            if winner not in winner_basenames:
                print(f"🔴 winner '{winner}' 不在反思集合里。已知 keys: {winner_basenames}")
                return 1
            parallel_winner_path = next(
                f for f in md_files
                if os.path.splitext(os.path.basename(f))[0] == winner
            )
            parallel_winner_path = normalize_path(parallel_winner_path)

    elif outcome == "fail":
        if not reflection:
            print("🔴 outcome=fail 必须给 --reflection <path>（sequential）或 --reflection-dir <dir>（parallel）")
            print("   sequential 推荐路径：docs/superpowers/plans/<plan>.reflections/loop-N.md")
            print("   parallel   推荐目录：docs/superpowers/plans/<plan>.reflections/loop-N.parallel/")
            return 1
        resolved, err = _validate_reflection_file(reflection)
        if err:
            print(err)
            print("   至少要包含：Hypothesis / Observed / Invalidation / Next Hypothesis / Decision")
            return 1
        reflection = normalize_path(resolved)

    now = datetime.now().isoformat()
    next_loop = st.get("loop_count", 0) + 1
    history = st.get("loop_history") or []
    entry = {
        "loop": next_loop,
        "outcome": outcome,
        "mode": mode,
        "ts": now,
    }
    if mode == "sequential":
        entry["reflection"] = reflection
    else:
        entry["reflection_dir"] = normalize_path(_resolve_path(reflection_dir))
        entry["reflections"] = parallel_files
        entry["winner"] = parallel_winner_path
    history.append(entry)

    st["loop_count"] = next_loop
    st["last_loop_outcome"] = outcome
    if mode == "sequential":
        st["last_loop_reflection"] = reflection
    else:
        st["last_loop_reflection"] = parallel_winner_path or entry["reflection_dir"]
    st["loop_history"] = history
    st["e2e_runs_since_last_record"] = 0
    save_state(st)

    if outcome == "pass":
        if mode == "parallel":
            print(f"✅ Loop {next_loop} (parallel) PASS — winner: {winner}")
            print(f"   {len(parallel_files)} 份反思保留在 {entry['reflection_dir']}")
        else:
            print(f"✅ Loop {next_loop} 记录为 PASS")
    else:
        if mode == "parallel":
            print(f"📝 Loop {next_loop} (parallel) FAIL — 全部 {len(parallel_files)} 个 hypothesis 都没过")
            print(f"   反思目录: {entry['reflection_dir']}")
        else:
            print(f"📝 Loop {next_loop} 记录为 FAIL（reflection: {reflection}）")
        if next_loop >= LOOP_LIMIT:
            print(f"🔴 已达 loop 上限 ({LOOP_LIMIT})，禁止继续重试 E2E。请向用户输出聚合分析。")
        elif next_loop >= 2:
            print("⚠️  下一轮反思必须包含 Circle Check（sequential：纵向对照；parallel：横向对照）。")
    return 0


def _tokenize_for_recall(text):
    """简易分词：英文取 ≥3 char word，中文取 bigram。"""
    if not text:
        return set()
    text = text.lower()
    word_tokens = re.findall(r'[a-z0-9]{3,}', text)
    cjk_runs = re.findall(r'[一-鿿]+', text)
    bigrams = []
    for run in cjk_runs:
        for i in range(len(run) - 1):
            bigrams.append(run[i:i+2])
    return set(word_tokens) | set(bigrams)


def _find_knowledge_files(repo_root):
    """递归查找所有 KNOWLEDGE.md（大小写不敏感），跳过常见的 vendor / build 目录。"""
    skip_dirs = {".git", "node_modules", "target", "dist", "build", ".venv", "venv", "__pycache__"}
    found = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".cache")]
        for fn in filenames:
            if fn.upper() == "KNOWLEDGE.MD":
                found.append(os.path.join(dirpath, fn))
    return sorted(found)


def _parse_knowledge_entries(file_path):
    """把 KNOWLEDGE.md 拆成 entry 列表。每个 entry = `## ...` 标题到下个 `## ` 之间。
    返回 [{"file": path, "line": N, "title": str, "body": str}]
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    entries = []
    cur_start = None
    cur_title = None
    cur_buf = []
    for idx, line in enumerate(lines, start=1):
        if line.startswith("## "):
            if cur_start is not None:
                entries.append({
                    "file": file_path, "line": cur_start,
                    "title": cur_title, "body": "".join(cur_buf).rstrip(),
                })
            cur_start = idx
            cur_title = line.rstrip("\n")
            cur_buf = [line]
        elif cur_start is not None:
            cur_buf.append(line)
    if cur_start is not None:
        entries.append({
            "file": file_path, "line": cur_start,
            "title": cur_title, "body": "".join(cur_buf).rstrip(),
        })
    return entries


def gate_knowledge_recall():
    """跨 session 学习：检索所有 KNOWLEDGE.md，返回与 query 相关的 top-N 条目原文。"""
    args = sys.argv[2:]
    query = None
    top = 5
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--query" and i + 1 < len(args):
            query = args[i + 1]; i += 2; continue
        if a.startswith("--query="):
            query = a.split("=", 1)[1]; i += 1; continue
        if a == "--top" and i + 1 < len(args):
            try: top = max(1, int(args[i + 1]))
            except ValueError: pass
            i += 2; continue
        if a.startswith("--top="):
            try: top = max(1, int(a.split("=", 1)[1]))
            except ValueError: pass
            i += 1; continue
        i += 1

    if not query or len(query.strip()) < 4:
        print("🔴 必须给 --query <需求一句话>（≥4 字符）")
        print('   例：knowledge_recall --query "Webhook 重发去重"')
        return 1

    repo_root = get_repo_root() or os.getcwd()
    files = _find_knowledge_files(repo_root)
    all_entries = []
    for f in files:
        all_entries.extend(_parse_knowledge_entries(f))

    q_tokens = _tokenize_for_recall(query)
    scored = []
    for entry in all_entries:
        e_tokens = _tokenize_for_recall(entry["title"] + "\n" + entry["body"])
        overlap = q_tokens & e_tokens
        if not overlap:
            continue
        score = len(overlap) / max(1, len(q_tokens))
        scored.append((score, len(overlap), entry))

    scored.sort(key=lambda x: (-x[0], -x[1]))
    matched = scored[:top]

    print(f"🧠 KNOWLEDGE Recall — query: \"{query}\"")
    print(f"   扫描 {len(files)} 个 KNOWLEDGE.md，共 {len(all_entries)} 条条目，命中 {len(scored)}")
    print()
    if matched:
        for score, _, entry in matched:
            rel = os.path.relpath(entry["file"], repo_root)
            print(f"──── {rel}:{entry['line']}  (score={score:.2f}) ────")
            print(entry["body"])
            print()
    else:
        print("（未找到相关条目——新项目或新方向是正常情况。仍标记 knowledge_recall_done=true）")

    branch = get_current_branch()
    st = ensure_branch_state(load_state(), branch)
    if not require_branch_match(st, branch):
        return 1
    st["knowledge_recall_done"] = True
    st["knowledge_recall_query"] = query.strip()
    st["knowledge_recall_count"] = len(matched)
    st["knowledge_recall_ts"] = datetime.now().isoformat()
    save_state(st)
    print(f"✅ knowledge_recall_done=true（命中 {len(matched)} 条，已记入 state）")
    return 0


VALID_REQUEST_TYPES = {"bugfix", "feature", "refactor", "optimize"}


def gate_classify():
    """1.0 需求分类 — 强制第一步。bugfix 自动激活 Bug 诊断 Gate。

    用法：classify --type bugfix|feature|refactor|optimize
    """
    args = sys.argv[2:]
    req_type = _extract_arg(args, "--type")
    if not req_type or req_type.lower() not in VALID_REQUEST_TYPES:
        print(f"🔴 必须给 --type <{'|'.join(sorted(VALID_REQUEST_TYPES))}>")
        print("   bugfix = 用户报告预期外行为/报错/数据不对/线上问题")
        print("   feature = 新增功能/页面/端点")
        print("   refactor = 重构/规范统一")
        print("   optimize = 性能/体验优化")
        return 1

    req_type = req_type.lower()
    branch = get_current_branch()
    st = ensure_branch_state(load_state(), branch)
    if not require_branch_match(st, branch):
        return 1
    now = datetime.now().isoformat()

    st["request_classified"] = True
    st["request_type"] = req_type
    st["request_classified_ts"] = now

    if req_type == "bugfix":
        st["bug_is_bugfix"] = True
        st["bug_diagnosis_done"] = False
        save_state(st)
        print(f"🐛 需求分类：bugfix — Bug 诊断 Gate 已激活（编辑代码前必须完成 D1→D2→D3）")
    else:
        st["bug_is_bugfix"] = False
        st["bug_diagnosis_done"] = True  # 非 bugfix 自动通过
        save_state(st)
        print(f"📋 需求分类：{req_type} — 正常流程（无 Bug 诊断 Gate）")

    return 0


def gate_bug_diagnosis():
    """记录 Bug 诊断 Gate 三步完成（1.1d）。
    Bugfix 场景下，编辑代码前必须先完成 D1 复现 + D2 根因 + D3 修复验证。

    子命令：
      bug_diagnosis mark_bugfix           → 标记当前需求为 Bugfix（激活诊断 Gate）
      bug_diagnosis reproduce --cmd "..." → D1 完成，记录复现命令
      bug_diagnosis root_cause --cause "..." → D2 完成，记录根因
      bug_diagnosis fix_verified          → D3 完成，修复假设已验证
      bug_diagnosis skip --reason "..."   → 非 Bugfix 场景显式跳过
    """
    args = sys.argv[2:]
    if not args:
        print("🔴 用法：bug_diagnosis <mark_bugfix|reproduce|root_cause|fix_verified|skip> [--cmd/--cause/--reason ...]")
        return 1

    subcmd = args[0]
    branch = get_current_branch()
    st = ensure_branch_state(load_state(), branch)
    if not require_branch_match(st, branch):
        return 1
    now = datetime.now().isoformat()

    if subcmd == "mark_bugfix":
        st["bug_is_bugfix"] = True
        st["bug_diagnosis_done"] = False
        save_state(st)
        print("🐛 已标记当前需求为 Bugfix — 编辑代码前必须完成 Bug 诊断 Gate（D1→D2→D3）")
        return 0

    if subcmd == "reproduce":
        cmd_val = _extract_arg(args[1:], "--cmd")
        if not cmd_val or len(cmd_val.strip()) < 10:
            print("🔴 必须给 --cmd '<实际执行的复现命令>'（≥10 字符）")
            print("   不能是'我读了代码觉得会报错'——必须是你实际跑过的命令")
            return 1
        st["bug_diagnosis_reproduce"] = cmd_val.strip()
        st["bug_diagnosis_ts"] = now
        save_state(st)
        print(f"✅ D1 复现已记录：{cmd_val.strip()[:80]}...")
        _check_diagnosis_complete(st)
        return 0

    if subcmd == "root_cause":
        cause = _extract_arg(args[1:], "--cause")
        if not cause or len(cause.strip()) < 15:
            print("🔴 必须给 --cause '<根因一句话>'（≥15 字符）")
            print("   必须基于 D1 的实际执行输出，不能只凭读代码推断")
            return 1
        st["bug_diagnosis_root_cause"] = cause.strip()
        st["bug_diagnosis_ts"] = now
        save_state(st)
        print(f"✅ D2 根因已记录：{cause.strip()[:80]}...")
        _check_diagnosis_complete(st)
        return 0

    if subcmd == "fix_verified":
        if not st.get("bug_diagnosis_reproduce"):
            print("🔴 D3 前必须先完成 D1（复现）")
            return 1
        if not st.get("bug_diagnosis_root_cause"):
            print("🔴 D3 前必须先完成 D2（根因）")
            return 1
        st["bug_diagnosis_fix_verified"] = True
        st["bug_diagnosis_done"] = True
        st["bug_diagnosis_ts"] = now
        save_state(st)
        print("✅ D3 修复假设验证已完成 — Bug 诊断 Gate 全部通过")
        return 0

    if subcmd == "skip":
        reason = _extract_arg(args[1:], "--reason")
        if not reason or len(reason.strip()) < 10:
            print("🔴 必须给 --reason '<≥10 字的原因>'")
            return 1
        st["bug_is_bugfix"] = False
        st["bug_diagnosis_done"] = True
        save_state(st)
        print(f"✅ Bug 诊断 Gate 跳过（非 Bugfix）：{reason.strip()}")
        return 0

    print(f"🔴 未知子命令：{subcmd}")
    print("   可用：mark_bugfix | reproduce | root_cause | fix_verified | skip")
    return 1


def _extract_arg(args, flag):
    """从 args 中提取 --flag value 或 --flag=value"""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(f"{flag}="):
            return a.split("=", 1)[1]
    return None


def _check_diagnosis_complete(st):
    """检查 D1+D2+D3 是否全部完成"""
    d1 = bool(st.get("bug_diagnosis_reproduce"))
    d2 = bool(st.get("bug_diagnosis_root_cause"))
    d3 = st.get("bug_diagnosis_fix_verified", False)
    done = [d1, d2, d3]
    labels = ["D1 复现", "D2 根因", "D3 修复验证"]
    remaining = [l for l, d in zip(labels, done) if not d]
    if remaining:
        print(f"   剩余步骤：{' → '.join(remaining)}")


def gate_knowledge_skip():
    """显式声明本次无新教训，跳过 KNOWLEDGE.md 更新（必须给 --reason）"""
    args = sys.argv[2:]
    reason = None
    for i, a in enumerate(args):
        if a == "--reason" and i + 1 < len(args):
            reason = args[i + 1]
            break
        if a.startswith("--reason="):
            reason = a.split("=", 1)[1]
            break
    if not reason or not reason.strip():
        print("🔴 必须给一个非空 --reason 才能跳过 KNOWLEDGE.md 更新")
        print('   例：python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" knowledge_skip \\')
        print("         --reason '纯文档改动，未触及代码或行为'")
        return 1
    if len(reason.strip()) < 10:
        print("🔴 --reason 太短（<10 字），请说人话")
        return 1
    branch = get_current_branch()
    st = ensure_branch_state(load_state(), branch)
    if not require_branch_match(st, branch):
        return 1
    now = datetime.now().isoformat()
    st["knowledge_acknowledged"] = True
    st["knowledge_skip_reason"] = reason.strip()
    st["knowledge_ts"] = now
    save_state(st)
    print(f"✅ Gate: 已记录跳过 KNOWLEDGE.md，原因：{reason.strip()}")
    return 0


def gate_post_bash():
    """PostToolUse: Bash — 检测测试/E2E 执行结果，写入 stamp（所有分支生效）"""
    branch = get_current_branch()

    data = read_stdin()
    cmd = data.get("tool_input", {}).get("command", "")
    output = extract_output(data)

    st = ensure_branch_state(load_state(), branch)
    if branch_mismatch(st, branch):
        return 0
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
            print(f"✅ Gate: {stack} test 通过，已记录")
        else:
            print(f"⚠️ Gate: {stack} test 已执行，但未检测到通过标志")

    # 检测 E2E 验证（必须成功才标记）
    if is_e2e_cmd(cmd) and not is_e2e_gate_cmd(cmd):
        ok, reason = e2e_succeeded(data)
        if ok:
            st["e2e_executed"] = True
            st["e2e_ts"] = now
            st["e2e_gate_passed"] = False
            st["e2e_gate_ts"] = None
            st["e2e_runs_since_last_record"] = st.get("e2e_runs_since_last_record", 0) + 1
            changed = True
            # Provenance: only hash if the command matches strict runner pattern
            if is_strict_e2e_runner(cmd) and os.path.exists(E2E_RESULT_PATH):
                try:
                    import hashlib
                    with open(E2E_RESULT_PATH, "rb") as f:
                        st["e2e_result_hash"] = hashlib.sha256(f.read()).hexdigest()
                    with open(E2E_RESULT_PATH, encoding="utf-8") as f:
                        rdata = json.load(f)
                    st["e2e_result_turns"] = sum(
                        len(r.get("turns", []))
                        for s in rdata.get("scenarios", [])
                        for r in s.get("rounds", [])
                    )
                    st["e2e_runner_cmd"] = cmd[:200]
                except Exception:
                    pass
            print(f"✅ Gate: E2E 验证通过（{reason}），loop {st.get('loop_count', 0) + 1} 进行中，待 loop_record")
        else:
            print(f"⚠️ Gate: E2E 命令已执行但未通过（{reason}），e2e_executed 保持 false")
            print("   请排查问题后重跑 E2E")

    # 检测 E2E Gate 脚本执行结果
    if is_e2e_gate_cmd(cmd):
        exit_code = extract_exit_code(data)
        if exit_code == 0:
            st["e2e_gate_passed"] = True
            st["e2e_gate_ts"] = now
            changed = True
            print("✅ Gate: E2E Gate 通过，已记录")
        elif exit_code is None:
            output = extract_output(data)
            if "GATE PASSED" in output:
                st["e2e_gate_passed"] = True
                st["e2e_gate_ts"] = now
                changed = True
                print("✅ Gate: E2E Gate 通过（via stdout），已记录")
            else:
                st["e2e_gate_passed"] = False
                st["e2e_gate_ts"] = None
                changed = True
                print("⚠️ Gate: E2E Gate 结果不明（无 exit code 且无 GATE PASSED），已清除旧状态")
        else:
            st["e2e_gate_passed"] = False
            st["e2e_gate_ts"] = None
            changed = True
            print(f"⚠️ Gate: E2E Gate 失败 (exit {exit_code})，e2e_gate_passed 已清除")

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
        print(f"⚠️ Gate 提醒: 合入/推送前仍需完成 → {', '.join(missing)}")

    return 0


def is_push_cmd(cmd):
    """检测 git push 命令"""
    if not cmd:
        return False
    return bool(re.search(r'\bgit\s+push\b', cmd))


def gate_pre_bash():
    """PreToolUse: Bash — 自动拦截，四层 Gate（所有分支生效）

    Gate 0: E2E 阶段 DB 写入 → 禁止（防止 fake 数据构造）
    Gate 1: E2E 命令 → 必须先跑通项目测试（单测）
    Gate 2: E2E Gate 脚本 → 必须项目测试 + E2E Runner 都完成
    Gate 3: merge/push → 必须项目测试 + E2E 都完成（已有逻辑）
    """
    branch = get_current_branch()

    data = read_stdin()
    cmd = data.get("tool_input", {}).get("command", "")

    st = ensure_branch_state(load_state(), branch)
    if branch_mismatch(st, branch) and not fastship_state.is_branch_recovery_command(cmd):
        print_branch_mismatch(st)
        return 1

    # --- Gate -1: 禁止 bash 写入 fastship state files ---
    if is_state_file_write_cmd(cmd):
        print("🔴 BLOCKED: fastship state file 由 hook 自动管理，禁止 shell 写入")
        print(f"   命令: {cmd[:100]}")
        return 1

    # --- Gate 0: E2E 阶段禁止直接 DB 写入 ---
    # 条件：项目测试已通过（说明进入了验证阶段），且检测到 DB 写入命令
    if st.get("test_passed") and is_db_write_cmd(cmd):
        lines = [
            "🔴 BLOCKED: E2E 阶段禁止直接操作数据库",
            "",
            "   检测到 DB 写入命令（INSERT/UPDATE/DELETE via psql）",
            "   E2E 验证必须使用当前环境的真实数据，不能手动构造",
            "",
            "   如果需要前置数据，请通过 API 调用产生",
            "   如果需要查询验证，请使用 SELECT（不会被拦截）",
        ]
        print("\n".join(lines))
        return 1

    # --- Gate 2: 跑 E2E Gate 前必须测试 + E2E Runner 都完成 ---
    # （必须在 Gate 1 之前，因为 e2e_gate 也匹配 is_e2e_cmd）
    if is_e2e_gate_cmd(cmd):
        blocks = []
        if not st.get("test_passed"):
            blocks.append("项目测试未通过")
        if not st.get("e2e_executed"):
            blocks.append("E2E Runner 未执行")
        if blocks:
            lines = ["🔴 BLOCKED: 验证未完成，禁止执行 E2E Gate"]
            for b in blocks:
                lines.append(f"   ❌ {b}")
            print("\n".join(lines))
            return 1
        return 0  # Gate 2 passed, skip Gate 1

    # --- Gate 1: 跑 E2E 前必须先通过项目测试 ---
    if is_e2e_cmd(cmd) and not is_e2e_gate_cmd(cmd) and not st.get("test_passed"):
        stacks = detect_stack()
        stack_hint = stacks[0] if stacks else "unknown"
        test_cmds = {
            "rust": "cargo test",
            "node": "npm test",
            "python": "pytest",
            "go": "go test ./...",
        }
        hint = test_cmds.get(stack_hint, "项目测试命令")
        lines = [
            "🔴 BLOCKED: 项目测试未通过，禁止执行 E2E",
            "",
            f"   请先运行项目测试: {hint}",
            "   测试通过后 Gate 自动放行 E2E",
        ]
        print("\n".join(lines))
        return 1

    # --- Gate 5: 重跑 E2E 前必须先 loop_record，失败时必须有 reflection ---
    if is_e2e_cmd(cmd) and not is_e2e_gate_cmd(cmd):
        unrecorded = st.get("e2e_runs_since_last_record", 0) >= 1
        loop_count = st.get("loop_count", 0)
        last_outcome = st.get("last_loop_outcome")

        if unrecorded:
            lines = [
                f"🔴 BLOCKED: 上一轮 E2E 未 loop_record，禁止再跑 E2E",
                "",
                "   先调用：",
                '     python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" loop_record \\',
                "       --outcome pass | fail [--reflection <path>]",
                "",
                "   失败时 reflection 路径推荐：",
                "     docs/superpowers/plans/<plan-name>.reflections/loop-N.md",
            ]
            print("\n".join(lines))
            return 1

        if last_outcome == "fail" and loop_count >= LOOP_LIMIT:
            lines = [
                f"🔴 BLOCKED: 已达 loop 上限 ({LOOP_LIMIT})，禁止继续重试 E2E",
                "",
                "   按 SKILL 3.2 — 第 3 次失败必须停下，输出聚合报告给用户：",
                "     - 3 次反思的根因对照",
                "     - 是否在原地打转（Circle Check 总结）",
                "     - 建议（方向调整 / 需要用户决策的问题）",
                "",
                "   如用户决定换方向、回阶段 1，先 reset：",
                '     python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" reset',
            ]
            print("\n".join(lines))
            return 1

    # --- Gate 3 + 4: merge/push 前必须全部完成 + KNOWLEDGE.md 已表态 ---
    if is_merge_cmd(cmd) or is_push_cmd(cmd):
        blocks = []
        if not st.get("test_passed"):
            blocks.append("项目测试未通过")
        if not st.get("e2e_executed"):
            blocks.append("E2E 验证未执行")
        if not st.get("e2e_gate_passed"):
            blocks.append("E2E Gate 未通过（e2e_gate.py 必须 exit 0）")
        if not st.get("knowledge_acknowledged"):
            blocks.append("KNOWLEDGE.md 未更新且未声明跳过")

        if blocks:
            action = "推送" if is_push_cmd(cmd) else "合入"
            lines = [
                f"🔴 BLOCKED: 验证未完成，禁止{action}",
            ]
            for b in blocks:
                lines.append(f"   ❌ {b}")
            lines.append("")
            lines.append("请先完成验证流程：")
            lines.append("   1. 项目测试（全量）")
            lines.append("   2. E2E 验证")
            lines.append("   3. KNOWLEDGE.md：")
            lines.append("      - 有新教训 → 编辑 KNOWLEDGE.md（hook 自动检测）")
            lines.append("      - 确实无新教训 → 显式声明跳过：")
            lines.append('          python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" knowledge_skip \\')
            lines.append("            --reason '<≥10 字的原因，例：纯文档改动，未触及代码或行为>'")
            print("\n".join(lines))
            return 1

        print("✅ Gate: 验证已完成，允许操作")

    return 0


def gate_status():
    """打印当前验证状态"""
    branch = get_current_branch()
    st = load_state()
    stacks = detect_stack()
    print(f"Branch:     {branch}")
    print(f"State for:  {st.get('branch', '-')}")
    if branch_mismatch(st, branch):
        print("Mismatch:   ⚠️ branch changed; gate mutation is paused until adopt-branch or reset")
    print(f"Stack:      {', '.join(stacks) if stacks else 'unknown'}")
    if st.get("request_classified"):
        print(f"Type:       📋 {st.get('request_type', '-')}  ({st.get('request_classified_ts', '-')})")
    else:
        print("Type:       ❌  (1.0 需求分类未完成 — 编辑代码会被 Gate B 拦)")
    t = "✅" if st.get("test_passed") else "❌"
    e = "✅" if st.get("e2e_executed") else "❌"
    p = "✅" if st.get("plan_ready") else ("⚠️ bypass" if st.get("plan_bypass") else "❌")
    tool = st.get("test_tool", "-")
    if st.get("knowledge_recall_done"):
        kr_line = f"✅  query=\"{st.get('knowledge_recall_query','-')}\"  hits={st.get('knowledge_recall_count',0)}  ({st.get('knowledge_recall_ts','-')})"
    else:
        kr_line = "❌  (1.1 未跑 knowledge_recall — 编辑代码会被 Gate B 拦)"
    if st.get("bug_is_bugfix"):
        d1 = "✅" if st.get("bug_diagnosis_reproduce") else "❌"
        d2 = "✅" if st.get("bug_diagnosis_root_cause") else "❌"
        d3 = "✅" if st.get("bug_diagnosis_fix_verified") else "❌"
        bd = "✅" if st.get("bug_diagnosis_done") else "❌"
        print(f"BugDiag:    {bd}  D1={d1} D2={d2} D3={d3}  ({st.get('bug_diagnosis_ts', '-')})")
        if st.get("bug_diagnosis_reproduce"):
            print(f"            D1 cmd: {st.get('bug_diagnosis_reproduce', '-')[:60]}...")
        if st.get("bug_diagnosis_root_cause"):
            print(f"            D2 cause: {st.get('bug_diagnosis_root_cause', '-')[:60]}...")
    else:
        print("BugDiag:    —  (非 Bugfix 或未标记)")
    print(f"Recall:     {kr_line}")
    print(f"Plan:       {p}  ({st.get('plan_file', '-')}) ({st.get('plan_ts', '-')})")
    print(f"Test:       {t}  ({tool}) ({st.get('test_ts', '-')})")
    print(f"E2E:        {e}  ({st.get('e2e_ts', '-')})")
    if st.get("knowledge_acknowledged"):
        if st.get("knowledge_file"):
            k_detail = f"updated: {st.get('knowledge_file')}"
        else:
            k_detail = f"skipped: {st.get('knowledge_skip_reason', '-')}"
        print(f"Knowledge:  ✅  ({k_detail}) ({st.get('knowledge_ts', '-')})")
    else:
        print(f"Knowledge:  ❌  (未更新 KNOWLEDGE.md，也未显式 knowledge_skip)")

    loop_count = st.get("loop_count", 0)
    last_outcome = st.get("last_loop_outcome")
    unrecorded = st.get("e2e_runs_since_last_record", 0)
    if loop_count == 0 and unrecorded == 0:
        loop_line = "—  (无 E2E 运行)"
    elif unrecorded:
        loop_line = f"⏳  ({unrecorded} 次 E2E 未 loop_record，下次 E2E 会被拦)"
    elif last_outcome == "pass":
        loop_line = f"✅  loop {loop_count} PASS"
    elif last_outcome == "fail":
        marker = "🔴 已达上限" if loop_count >= LOOP_LIMIT else "⚠️  可重试"
        loop_line = f"❌  loop {loop_count} FAIL ({marker})  reflection={st.get('last_loop_reflection', '-')}"
    else:
        loop_line = f"-   ({loop_count} 个已记录 loop)"
    print(f"Loop:       {loop_line}")
    history = st.get("loop_history") or []
    if history:
        for h in history:
            mode = h.get("mode", "sequential")
            if mode == "parallel":
                hypotheses = h.get("reflections") or []
                winner = h.get("winner")
                marker = f"winner={os.path.basename(winner)}" if winner else f"all-fail ({len(hypotheses)})"
                print(f"            └ loop {h.get('loop')} [parallel]: {h.get('outcome')}  {marker}  {h.get('ts','')}")
                for r in hypotheses:
                    tag = "★" if winner and r == winner else " "
                    print(f"               {tag} {r}")
            else:
                print(f"            └ loop {h.get('loop')} [sequential]: {h.get('outcome')}  ({h.get('reflection') or '-'})  {h.get('ts','')}")
    return 0


def gate_reset():
    """手动重置验证状态（新需求开始时调用）"""
    branch = get_current_branch()
    st = empty_state(branch)
    save_state(st)
    legacy = fastship_state.legacy_gate_state_path()
    if os.path.exists(legacy):
        os.remove(legacy)
    print(f"✅ Gate: 验证状态已重置 (branch: {branch})")
    return 0


# ---------- 入口 ----------

def main():
    if len(sys.argv) < 2:
        print("Usage: ship_verify_gate.py <post_bash|post_edit|pre_bash|pre_edit|status|reset|plan_bypass|knowledge_skip|knowledge_recall|loop_record>")
        sys.exit(1)

    handlers = {
        "post_bash": gate_post_bash,
        "post_edit": gate_post_edit,
        "pre_bash": gate_pre_bash,
        "pre_edit": gate_pre_edit,
        "status": gate_status,
        "reset": gate_reset,
        "plan_bypass": gate_plan_bypass,
        "knowledge_skip": gate_knowledge_skip,
        "knowledge_recall": gate_knowledge_recall,
        "loop_record": gate_loop_record,
        "bug_diagnosis": gate_bug_diagnosis,
        "classify": gate_classify,
    }
    handler = handlers.get(sys.argv[1])
    if handler:
        sys.exit(handler())
    else:
        print(f"Unknown action: {sys.argv[1]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
