#!/usr/bin/env python3
"""
fastship_orchestrator.py — Hook entry point + state machine for /fastship.

Dual-mode:
  Hook mode (called by settings.local.json hooks, reads stdin):
    pre_edit / pre_bash / post_edit / post_bash
  CLI mode (called by Claude/Codex for manual steps):
    start / done / next / status / reset

Delegates to ship_verify_gate.py (subprocess) for low-level gate enforcement.
"""

import sys
import os
import json
import re
import subprocess
import hashlib
import shutil
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable, Any, Union

import fastship_state


# ━━━━━━━━━━━━ Context Compact Advisory ━━━━━━━━━━━━

COMPACT_RECENCY_SECS = int(os.environ.get("FASTSHIP_COMPACT_RECENCY", "120"))


def _compaction_log_paths() -> list:
    """Candidate compaction.log locations, most-authoritative first.

    /compact writes to the MAIN worktree's .claude/checkpoints/compaction.log.
    Inside a linked worktree, _repo_root() points at the worktree whose own log
    is stale; consult the shared (main-worktree) log via git-common-dir as well
    and take the most recent across both. Main-repo behaviour is unchanged since
    the two paths coincide there. Mirrors the forge_gate fix for the same bug.
    """
    paths = []
    seen = set()

    def _add(p):
        if p and p not in seen:
            seen.add(p)
            paths.append(p)

    common = fastship_state.git_common_dir()
    if common:
        _add(os.path.join(os.path.dirname(common), ".claude", "checkpoints", "compaction.log"))
    _add(os.path.join(_repo_root(), ".claude", "checkpoints", "compaction.log"))
    return paths


def _last_compaction_epoch() -> float:
    best = 0.0
    for log in _compaction_log_paths():
        try:
            with open(log, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 256))
                last_line = f.read().decode().strip().rsplit("\n", 1)[-1]
                ts = last_line.split(" ", 1)[0]
                best = max(best, datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
    return best


def _compact_is_recent() -> bool:
    age = _time.time() - _last_compaction_epoch()
    return 0 <= age < COMPACT_RECENCY_SECS


# ━━━━━━━━━━━━ State Management ━━━━━━━━━━━━

def _repo_root():
    return fastship_state.repo_root()


def orch_state_path():
    return fastship_state.orchestrator_state_path()


def hook_state_path():
    return fastship_state.gate_state_path()


def gate_script_path():
    return fastship_state.gate_script_path()


def _localize_cli(text: str) -> str:
    """Rewrite printed CLI hints to the engine's REAL resolved location.

    The step instructions and prompts are authored with the source/legacy
    invocation forms (``"$(git rev-parse --show-toplevel)/.claude/tools/fastship"``
    and ``.claude/hooks/ship_verify_gate.py``). Those paths do not exist when the
    engine is installed as a Claude Code plugin (it lives under the plugin cache),
    and the legacy ``.claude/hooks/ship_verify_gate.py`` copy has been removed.
    Rewrite them at print time to the orchestrator's own abspath and the resolved
    gate-script path, which are correct in every layout (source / plugin / legacy).
    """
    if not isinstance(text, str):
        return text  # callable/None step instructions pass through untouched
    self_cli = 'python3 "%s"' % os.path.abspath(__file__)
    gate_cli = 'python3 "%s"' % fastship_state.gate_script_path()
    text = text.replace(
        'python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py"',
        gate_cli)
    text = text.replace(
        '"$(git rev-parse --show-toplevel)/.claude/tools/fastship"',
        self_cli)
    return text


def _current_branch() -> Optional[str]:
    return fastship_state.current_branch()


def empty_orchestrator_state(requirement: str) -> dict:
    return {
        "requirement": requirement,
        "request_type": None,
        "current_step": "1.0",
        "completed_steps": [],
        "skipped_steps": [],
        "phase": 1,
        "branch": _current_branch(),
        "repo_root": _repo_root(),
        "brief_path": None,
        "plan_path": None,
        "report_path": None,
        "started_at": datetime.now().isoformat(),
        "loop_count": 0,
        "artifacts": {},
    }


def save_orch_state(st: dict, path: str = None):
    p = path or orch_state_path()
    if path is None:
        fastship_state.update_session_from_state(st)
    fastship_state.save_json(p, st)


def save_hook_state(st: dict, path: str = None):
    p = path or hook_state_path()
    fastship_state.save_json(p, st)


def load_orch_state(path: str = None) -> Optional[dict]:
    """Load orchestrator state. Returns None only if no readable state exists."""
    if path is None:
        fastship_state.migrate_legacy_state("orchestrator")
    p = path or orch_state_path()
    return fastship_state.load_json(p)


def load_hook_state(path: str = None) -> dict:
    if path is None:
        fastship_state.migrate_legacy_state("gate")
    p = path or hook_state_path()
    return fastship_state.load_json(p) or {}


def _branch_mismatch(st: Optional[dict]) -> bool:
    return fastship_state.branch_mismatch(st)


def _branch_mismatch_text(st: dict) -> str:
    return "\n".join(fastship_state.branch_mismatch_lines(st))


def _is_active(st: Optional[dict]) -> bool:
    return bool(st and st.get("current_step") not in ("done", "stopped", None))


# ━━━━━━━━━━━━ Gate Delegation ━━━━━━━━━━━━

def delegate_to_gate(gate_path: str, action: str, data: dict) -> tuple:
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
    instruction: Union[str, Callable[[dict], str]]
    validator: Callable[[dict, dict], tuple]
    done_flags: list = field(default_factory=list)
    conditional: Optional[str] = None


# ━━━━━━━━━━━━ Validators (state-bound; no filesystem fallback) ━━━━━━━━━━━━

BRIEF_FILENAME = ".fastship-brief.md"
PLAN_DIR_MARKER = "docs/superpowers/plans/"
E2E_RESULT_PATH = ".claude/fastship-e2e-result.json"
E2E_MIN_TURNS = 10
GRILL_RESULT_FILENAME = ".fastship-grill-result.md"
CODEX_REVIEW_FILENAME = ".fastship-codex-review.md"
CODE_REVIEW_FILENAME = ".fastship-code-review.md"
REQUIREMENTS_FILENAME = ".fastship-requirements.md"

# ── plan.html visualization (derived, non-gating view of the 1.4 plan) ──
_PLAN_HTML_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "scripts", "plan_html.py")


def _load_plan_html_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location("plan_html", _PLAN_HTML_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def generate_plan_html(plan_path: str):
    """Render plan.md -> sibling .plan.html. Best-effort: returns out path or None.
    Never raises — a render failure must NOT block the 1.4 step (visualization is
    a derived view, not a gated deliverable)."""
    try:
        if not plan_path or not os.path.exists(plan_path):
            return None
        mod = _load_plan_html_mod()
        return mod.render_plan_file(plan_path)
    except Exception as e:  # noqa: BLE001 — visualization is non-critical
        print(f"⚠️ plan.html 生成失败（不阻断）: {e}")
        return None


def attach_plan_html(orch: dict, plan_path: str):
    """Generate the HTML, record its path in NON-trusted artifacts (never the ledger),
    and best-effort auto-open it in the browser so the plan is shown, not just written.
    Opening is gated by FASTSHIP_PLAN_HTML_OPEN (auto/never/always) and never blocks."""
    out = generate_plan_html(plan_path)
    if out:
        orch.setdefault("artifacts", {})["plan_html_path"] = out
        print(f"🖼️  plan.html: {out}")
        try:
            if _load_plan_html_mod().open_in_browser(out):
                print("   ↳ 已在浏览器打开（关闭自动打开：export FASTSHIP_PLAN_HTML_OPEN=never）")
        except Exception:  # noqa: BLE001 — opening is best-effort
            pass
    return out

STEP_ARTIFACT_OWNERS = {
    BRIEF_FILENAME: "1.3",
    REQUIREMENTS_FILENAME: "1.3r",
    GRILL_RESULT_FILENAME: "1.5",
    CODEX_REVIEW_FILENAME: "1.5c",
    CODE_REVIEW_FILENAME: "2.5",
}


def _artifact_owner_step(file_path: str) -> Optional[str]:
    """Return the step ID that owns this artifact, or None if not a step artifact."""
    p = _normalize(file_path)
    for marker, step_id in STEP_ARTIFACT_OWNERS.items():
        if marker in p:
            return step_id
    if PLAN_DIR_MARKER in p and p.endswith(".md"):
        return "1.4"
    if p.endswith("/knowledge.md") or os.path.basename(p).upper() == "KNOWLEDGE.MD":
        return "3.6"
    return None


PLAN_SIGNATURE_MARKERS = [
    "For agentic workers",
    "**Goal:**",
    "- [ ] **Step",
]

GRILL_REQUIRED_SECTIONS = ["拷问", "修订", "结论"]
CODEX_GATE_RE = re.compile(r"#+\s*GATE:\s*(PASS|FAIL)\b", re.IGNORECASE)
CODEX_GATE_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.IGNORECASE | re.DOTALL)
TRUSTED_ARTIFACTS_KEY = "trusted_artifacts"
CODEX_REVIEW_PLAN_HASH_FIELD = "reviewed_plan_sha256"
CODEX_REVIEW_REQUIRED_TRUE_FIELDS = (
    "p0_contract_reviewed",
    "ac_e2e_coverage_reviewed",
    "weak_case_reviewed",
    "evidence_plan_reviewed",
)
CODEX_REVIEW_REQUIRED_EMPTY_FIELDS = (
    "p0_requirements_missing",
    "uncovered_ac",
    "unmapped_e2e_scenarios",
    "weak_scenarios",
    "non_business_assertions",
    "missing_evidence",
)

# ── Code Review (Step 2.5) gate contract — reviews the IMPLEMENTATION, not the plan ──
# Binding is to the design source (reviewed_against) + changed files, NOT a plan hash:
# a code review checks the implementation against the design, so plan-hash binding
# (as 1.5c uses) would be meaningless here.
CODE_REVIEW_REQUIRED_TRUE_FIELDS = (
    "design_fidelity_reviewed",
    "spec_compliance_reviewed",
    "quality_reviewed",
)
CODE_REVIEW_REQUIRED_EMPTY_FIELDS = (
    "design_deviations",
    "spec_gaps",
    "quality_issues",
    "unverified_claims",
)


def _absolute_path(path: str) -> str:
    if not path:
        return ""
    if not os.path.isabs(path):
        path = os.path.join(_repo_root(), path)
    return os.path.realpath(path)


def _project_e2e_config() -> dict:
    cfg = fastship_state.load_project_config()
    e2e = cfg.get("e2e") if isinstance(cfg, dict) else None
    return e2e if isinstance(e2e, dict) else {}


def _config_str(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _config_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _config_list(value: Any) -> list:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = [value]
    else:
        return []
    cleaned = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            cleaned.append(text)
    return cleaned


def _e2e_result_path() -> str:
    path = _config_str(_project_e2e_config().get("result_path"), E2E_RESULT_PATH)
    if not os.path.isabs(path):
        path = os.path.join(_repo_root(), path)
    return os.path.abspath(path)


def _e2e_min_turns() -> int:
    return _config_int(_project_e2e_config().get("min_turns"), E2E_MIN_TURNS)


def _e2e_setup_commands() -> list:
    return _config_list(_project_e2e_config().get("setup_commands"))


def _e2e_notes() -> list:
    return _config_list(_project_e2e_config().get("notes"))


def _e2e_runner_command() -> str:
    default = f"python3 tests/e2e_runner.py -o {_e2e_result_path()}"
    return _config_str(_project_e2e_config().get("runner_command"), default)


def _e2e_gate_command() -> str:
    default = f"python3 tests/e2e_gate.py --result {_e2e_result_path()} --min-turns {_e2e_min_turns()}"
    return _config_str(_project_e2e_config().get("gate_command"), default)


def _command_block(commands: list) -> str:
    return "\n".join(f"  {cmd}" for cmd in commands)


def _e2e_runner_instruction(_orch: dict = None) -> str:
    lines = ["运行项目 E2E Runner 采集数据："]
    setup = _e2e_setup_commands()
    if setup:
        lines.extend(["", "准备服务（按顺序执行，保持服务可用）：", _command_block(setup)])
    lines.extend(["", "采集数据：", f"  {_e2e_runner_command()}"])
    notes = _e2e_notes()
    if notes:
        lines.extend(["", "项目说明："])
        lines.extend(f"- {note}" for note in notes)
    lines.append(f"🔴 最少 {_e2e_min_turns()} 轮。Runner 只采集不判断。orchestrator 自动检测。")
    lines.append(f"🔴 原始结果必须写入 {_e2e_result_path()}，hook/gate 会记录 hash。")
    return "\n".join(lines)


def _e2e_report_instruction(_orch: dict = None) -> str:
    result_path = _e2e_result_path()
    return f"""读 {result_path}，写 E2E 质量检测报告到文件。
报告含: 覆盖度 / 逐轮审查(完整输出) / 总结 / gate.json 中的 e2e_result_hash。
🔴 通过率 < 80% 或 AC 未覆盖 → 不合入。
🔴 Validator 自动验证 {result_path} 完整性（hash 比对 gate.json 记录）。
🔴 禁止手动创建或修改 {result_path}。
用 Write 工具保存报告。orchestrator 自动检测文件写入。"""


def _e2e_gate_instruction(_orch: dict = None) -> str:
    return f"""运行 Gate 脚本：
  {_e2e_gate_command()}
Gate 展示原始数据给用户对照。FAIL → 禁止合入。
🔴 Validator 以子进程方式运行 e2e_gate.py，检查 exit code。
🔴 Gate 必须 exit 0 才能通过，exit 非 0 自动拦截。
🔴 Auto-detection 同时验证 exit code，命令失败不推进。"""


def _file_fingerprint(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            size += len(chunk)
            h.update(chunk)
    return h.hexdigest(), size


def _trusted_artifacts(orch: dict) -> dict:
    return orch.setdefault("artifacts", {}).setdefault(TRUSTED_ARTIFACTS_KEY, {})


def record_step_artifact(orch: dict, step_id: str, path: str, source: str = "hook") -> tuple[bool, str]:
    """Record current-step artifact provenance in orchestrator state."""
    abs_path = _absolute_path(path)
    if not abs_path or not os.path.exists(abs_path):
        return False, f"artifact 不存在: {path}"
    try:
        digest, size = _file_fingerprint(abs_path)
    except OSError as e:
        return False, f"artifact fingerprint 失败: {e}"
    _trusted_artifacts(orch)[step_id] = {
        "step_id": step_id,
        "path": abs_path,
        "sha256": digest,
        "size": size,
        "source": source,
        "recorded_at": datetime.now().isoformat(),
    }
    return True, digest


def _clear_trusted_artifacts(orch: dict, step_ids: tuple[str, ...]):
    trusted = orch.setdefault("artifacts", {}).get(TRUSTED_ARTIFACTS_KEY, {})
    for step_id in step_ids:
        trusted.pop(step_id, None)


def _verify_step_artifact(orch: dict, step_id: str, path: str) -> tuple[bool, str, dict]:
    trusted = orch.get("artifacts", {}).get(TRUSTED_ARTIFACTS_KEY, {})
    rec = trusted.get(step_id)
    if not rec:
        return False, f"Step {step_id} artifact 缺少可信 provenance/hash 记录", {}
    abs_path = _absolute_path(path)
    rec_path = _absolute_path(rec.get("path", ""))
    if not abs_path or abs_path != rec_path:
        return False, f"Step {step_id} artifact 路径与 provenance 不一致", rec
    if rec.get("step_id") != step_id:
        return False, f"Step {step_id} artifact provenance step 不一致", rec
    if not os.path.exists(abs_path):
        return False, f"Step {step_id} artifact 文件不存在: {path}", rec
    try:
        digest, size = _file_fingerprint(abs_path)
    except OSError as e:
        return False, f"Step {step_id} artifact fingerprint 失败: {e}", rec
    if digest != rec.get("sha256") or size != rec.get("size"):
        return False, f"Step {step_id} artifact hash/size mismatch — 文件记录后被修改", rec
    return True, "trusted artifact verified", rec


def _read_gate_state_file() -> dict:
    p = hook_state_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


def _git_head_sha() -> Optional[str]:
    """Best-effort HEAD sha recorded at session start as the code-review diff base."""
    try:
        r = subprocess.run(
            ["git", "-C", _repo_root(), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        sha = r.stdout.strip()
        return sha or None
    except Exception:
        return None


def _changed_files(base_sha: Optional[str] = None) -> set:
    """Best-effort set of repo-relative paths changed in this feature.

    Union of uncommitted (working tree + staged) and, when a base sha was
    recorded at session start, everything committed since the base. Returns an
    empty set when git is unavailable (e.g. tests) so callers can skip the
    diff-intersection check rather than fail spuriously.
    """
    root = _repo_root()
    files: set = set()
    cmds = [
        ["git", "-C", root, "diff", "--name-only", "HEAD"],
        ["git", "-C", root, "diff", "--name-only", "--cached"],
    ]
    if base_sha:
        cmds.append(["git", "-C", root, "diff", "--name-only", f"{base_sha}...HEAD"])
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                line = line.strip()
                if line:
                    files.add(_normalize(line))
        except Exception:
            pass
    return files


def validate_classify(orch: dict, hook: dict) -> tuple:
    if hook.get("request_classified"):
        return True, f"classified: {hook.get('request_type')}"
    gate = _read_gate_state_file()
    if gate.get("request_classified"):
        return True, f"classified: {gate.get('request_type')} (via state file)"
    return False, "运行 classify CLI 注册需求类型"


def validate_recall(orch: dict, hook: dict) -> tuple:
    if hook.get("knowledge_recall_done"):
        return True, f"recall done (hits={hook.get('knowledge_recall_count', 0)})"
    gate = _read_gate_state_file()
    if gate.get("knowledge_recall_done"):
        return True, f"recall done (hits={gate.get('knowledge_recall_count', 0)}, via state file)"
    return False, "运行 knowledge_recall CLI"


def validate_explore(orch: dict, hook: dict) -> tuple:
    n = orch.get("artifacts", {}).get("explore_agents", 0)
    if n >= 3:
        return True, f"{n} agents dispatched"
    return False, f"需要 ≥3 个 Explore subagent (当前: {n})"


def validate_brief(orch: dict, hook: dict) -> tuple:
    path = orch.get("brief_path")
    if not path:
        return False, "brief_path 未由当前 step 写入记录，禁止 filesystem fallback"
    ok, msg, _rec = _verify_step_artifact(orch, "1.3", path)
    if not ok:
        return False, msg
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


def validate_diagnosis(orch: dict, hook: dict) -> tuple:
    if orch.get("request_type") != "bugfix":
        return True, "非 bugfix，跳过"
    if hook.get("bug_diagnosis_done"):
        return True, "D1+D2+D3 完成"
    gate = _read_gate_state_file()
    if gate.get("bug_diagnosis_done"):
        return True, "D1+D2+D3 完成 (via state file)"
    return False, "Bug 诊断 Gate 未完成"


def validate_plan(orch: dict, hook: dict) -> tuple:
    plan_path = orch.get("plan_path")
    if not plan_path:
        return False, "plan_path 未由当前 step 写入记录，禁止 filesystem fallback"
    if not os.path.isabs(plan_path):
        plan_path = os.path.join(_repo_root(), plan_path)
    normalized = _normalize(plan_path)
    if PLAN_DIR_MARKER not in normalized or not normalized.endswith(".md"):
        return False, f"plan_path 非合法 plan 产物: {plan_path}"
    if not os.path.exists(plan_path):
        return False, f"plan 文件不存在: {plan_path}"
    ok, msg, _rec = _verify_step_artifact(orch, "1.4", plan_path)
    if not ok:
        return False, msg
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


def validate_grill(orch: dict, hook: dict) -> tuple:
    path = orch.get("artifacts", {}).get("grill_result_path")
    if not path:
        return False, "grill_result_path 未由当前 step 写入记录，禁止 filesystem fallback"
    ok, msg, _plan_rec = _verify_step_artifact(orch, "1.4", orch.get("plan_path"))
    if not ok:
        return False, "Grill 无可信 plan 输入: " + msg
    ok, msg, _rec = _verify_step_artifact(orch, "1.5", path)
    if not ok:
        return False, msg
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


def validate_codex_review(orch: dict, hook: dict) -> tuple:
    path = orch.get("artifacts", {}).get("codex_review_path")
    if not path:
        return False, "codex_review_path 未由当前 step 写入记录，禁止 filesystem fallback"
    plan_ok, plan_msg, plan_rec = _verify_step_artifact(orch, "1.4", orch.get("plan_path"))
    if not plan_ok:
        return False, "Codex review 无可信 plan 输入: " + plan_msg
    ok, msg, _rec = _verify_step_artifact(orch, "1.5c", path)
    if not ok:
        return False, msg
    if not os.path.isabs(path):
        path = os.path.join(_repo_root(), path)
    if CODEX_REVIEW_FILENAME not in _normalize(path):
        return False, f"Codex review 路径非法: {path}"
    if not os.path.exists(path):
        return False, (
            f"Codex review 结果不存在: {CODEX_REVIEW_FILENAME}。"
            f"调用 Skill(skill='codex') review plan，完成后写结果到 .claude/{CODEX_REVIEW_FILENAME}"
        )
    try:
        content = open(path, encoding="utf-8").read()
    except Exception:
        return False, f"无法读取: {path}"
    if len(content) < 100:
        return False, f"Codex review 太短 ({len(content)}B < 100B)"
    m = CODEX_GATE_RE.search(content)
    if not m:
        return False, "Codex review 缺少显式 GATE 判定行（格式: ### GATE: PASS 或 ### GATE: FAIL）"
    verdict = m.group(1).upper()
    if verdict == "FAIL":
        return False, "codex review FAIL — 需更新 plan 后重新 review（orchestrator 自动回退到 1.4）"

    matches = CODEX_GATE_JSON_RE.findall(content)
    if not matches:
        return False, "Codex review 缺少机器可验证 JSON gate，禁止纯文本 PASS"
    try:
        gate = json.loads(matches[-1])
    except json.JSONDecodeError as e:
        return False, f"Codex review JSON gate 解析失败: {e}"
    if not isinstance(gate, dict):
        return False, "Codex review JSON gate 必须是 object"

    json_verdict = str(gate.get("gate", "")).upper()
    if json_verdict not in {"PASS", "FAIL"}:
        return False, "Codex review JSON gate 缺少 gate=PASS/FAIL"
    if json_verdict != verdict:
        return False, f"Codex review 文本 GATE={verdict} 与 JSON gate={json_verdict} 不一致"
    if json_verdict == "FAIL":
        return False, "codex review JSON gate FAIL — 需更新 plan 后重新 review"

    reviewed_hash = gate.get(CODEX_REVIEW_PLAN_HASH_FIELD)
    if reviewed_hash != plan_rec.get("sha256"):
        return False, "Codex review 未绑定当前 plan hash，禁止复用/伪造旧 review"

    missing_true = [field for field in CODEX_REVIEW_REQUIRED_TRUE_FIELDS if gate.get(field) is not True]
    if missing_true:
        return False, "Codex review 未确认硬审查项: " + ", ".join(missing_true)

    missing_lists = [
        field for field in CODEX_REVIEW_REQUIRED_EMPTY_FIELDS
        if field not in gate or not isinstance(gate.get(field), list)
    ]
    if missing_lists:
        return False, "Codex review JSON gate 缺少数组字段: " + ", ".join(missing_lists)

    unresolved = [field for field in CODEX_REVIEW_REQUIRED_EMPTY_FIELDS if gate.get(field)]
    if unresolved:
        return False, "Codex review 存在未解决覆盖/证据问题: " + ", ".join(unresolved)

    return True, "codex review PASS (structured gate verified)"


def validate_code_review(orch: dict, hook: dict) -> tuple:
    """Step 2.5 gate — adversarial review of the IMPLEMENTATION (not the plan).

    Mirrors the 1.5c structured-gate contract but binds the review to the design/
    spec artifact it compared against (reviewed_against) and to the files it
    actually inspected (reviewed_files ∩ git diff). This is the defense that
    catches "tests pass but it doesn't match the design" failures: empty
    design_deviations is required, and the review must name a real design source.
    """
    path = orch.get("artifacts", {}).get("code_review_path")
    if not path:
        return False, "code_review_path 未由当前 step 写入记录，禁止 filesystem fallback"
    ok, msg, _rec = _verify_step_artifact(orch, "2.5", path)
    if not ok:
        return False, msg
    if not os.path.isabs(path):
        path = os.path.join(_repo_root(), path)
    if CODE_REVIEW_FILENAME not in _normalize(path):
        return False, f"Code review 路径非法: {path}"
    if not os.path.exists(path):
        return False, (
            f"Code review 结果不存在: {CODE_REVIEW_FILENAME}。"
            f"对实现做对抗性 review（设计稿保真度 + spec 合同 + 质量），写结果到 .claude/{CODE_REVIEW_FILENAME}"
        )
    try:
        content = open(path, encoding="utf-8").read()
    except Exception:
        return False, f"无法读取: {path}"
    if len(content) < 200:
        return False, f"Code review 太短 ({len(content)}B < 200B)，需逐 task review + 设计稿保真度比对"

    m = CODEX_GATE_RE.search(content)
    if not m:
        return False, "Code review 缺少显式 GATE 判定行（格式: ### GATE: PASS 或 ### GATE: FAIL）"
    verdict = m.group(1).upper()
    if verdict == "FAIL":
        return False, "code review FAIL — 修复实现后重新 review（留在 2.5，不回退 plan）"

    matches = CODEX_GATE_JSON_RE.findall(content)
    if not matches:
        return False, "Code review 缺少机器可验证 JSON gate，禁止纯文本 PASS"
    try:
        gate = json.loads(matches[-1])
    except json.JSONDecodeError as e:
        return False, f"Code review JSON gate 解析失败: {e}"
    if not isinstance(gate, dict):
        return False, "Code review JSON gate 必须是 object"

    json_verdict = str(gate.get("gate", "")).upper()
    if json_verdict not in {"PASS", "FAIL"}:
        return False, "Code review JSON gate 缺少 gate=PASS/FAIL"
    if json_verdict != verdict:
        return False, f"Code review 文本 GATE={verdict} 与 JSON gate={json_verdict} 不一致"
    if json_verdict == "FAIL":
        return False, "code review JSON gate FAIL — 修复实现后重新 review"

    missing_true = [f for f in CODE_REVIEW_REQUIRED_TRUE_FIELDS if gate.get(f) is not True]
    if missing_true:
        return False, "Code review 未确认硬审查项: " + ", ".join(missing_true)

    missing_lists = [
        f for f in CODE_REVIEW_REQUIRED_EMPTY_FIELDS
        if f not in gate or not isinstance(gate.get(f), list)
    ]
    if missing_lists:
        return False, "Code review JSON gate 缺少数组字段: " + ", ".join(missing_lists)
    unresolved = [f for f in CODE_REVIEW_REQUIRED_EMPTY_FIELDS if gate.get(f)]
    if unresolved:
        return False, "Code review 存在未解决问题: " + ", ".join(unresolved)

    # Design-fidelity anchor: the review must compare against a real design/spec source.
    reviewed_against = gate.get("reviewed_against")
    if not reviewed_against or not isinstance(reviewed_against, str):
        return False, "Code review 缺少 reviewed_against（设计稿/spec 路径，禁止对着空气 review）"
    ra_abs = reviewed_against if os.path.isabs(reviewed_against) else os.path.join(_repo_root(), reviewed_against)
    if not os.path.exists(ra_abs):
        return False, f"Code review reviewed_against 指向的设计依据不存在: {reviewed_against}"

    # Anti-rubber-stamp: reviewed_files must be real and intersect the actual diff.
    reviewed_files = gate.get("reviewed_files")
    if not isinstance(reviewed_files, list) or not reviewed_files:
        return False, "Code review 缺少 reviewed_files（实际审查的文件列表）"
    for rf in reviewed_files:
        if not isinstance(rf, str):
            return False, "Code review reviewed_files 含非字符串项"
        rf_abs = rf if os.path.isabs(rf) else os.path.join(_repo_root(), rf)
        if not os.path.exists(rf_abs):
            return False, f"Code review reviewed_files 含不存在的文件: {rf}"
    changed = _changed_files(orch.get("base_sha"))
    if changed:
        changed_base = {os.path.basename(c) for c in changed}
        reviewed_base = {os.path.basename(_normalize(rf)) for rf in reviewed_files}
        if not (changed_base & reviewed_base):
            return False, "Code review 未覆盖任何实际改动文件（reviewed_files 与 git diff 不相交）"

    return True, "code review PASS (structured gate verified)"


# ── 1A Requirements tribunal (Step 1.3r) contract ──────────────────────────────
# .fastship-requirements.md is the 需求定稿 produced by the Phase-1 1A multi-role
# tribunal (产品/运营/数据/财务 → 书记员合成 → grill). Its synthesis discipline is
# ENGINE-enforced here, not by skill prompts: the 书记员 is a clerk, never a judge.
# The verdict is DERIVED from structure — a self-declared "gate":"PASS" is ignored.
REQUIREMENTS_STEP_ID = "1.3r"
REQUIREMENTS_REQUIRED_LIST_FIELDS = ("roles", "additive_union", "exclusive_forks", "p0")


def _check_requirements_contract(gate: dict) -> tuple:
    """Pure discipline check over the requirements-lock JSON contract — no I/O, no
    ledger, no hashing — so the synthesis rules are unit-testable in isolation.

    FAIL (with a specific reason) on any of:
      - structure: a required list field is missing / wrong type;
      - role integrity: abstain ⇒ no concerns; a non-abstaining concern ⇒ non-empty
        evidence_ref (blocks fabricated requirements); ≥1 non-abstaining role;
      - additive 并集不减: a non-abstaining role concern id missing from
        additive_union (the synthesizer silently dropped it — the exact failure the
        dogfood tribunal exhibited);
      - exclusive forks: any fork still open (held until grill arbitrates), or a
        resolved fork without a resolution;
      - completeness: ≥1 P0, each P0 carries a source and ≥1 observable AC.
    """
    if not isinstance(gate, dict):
        return False, "requirements 契约必须是 JSON object"
    for f in REQUIREMENTS_REQUIRED_LIST_FIELDS:
        if not isinstance(gate.get(f), list):
            return False, f"requirements 契约缺少数组字段或类型错误: {f}"

    role_concern_ids = set()
    non_abstaining = 0
    for r in gate["roles"]:
        if not isinstance(r, dict):
            return False, "roles 含非 object 项"
        name = r.get("role")
        if not isinstance(name, str) or not name.strip():
            return False, "role 缺少非空 role 名"
        if not isinstance(r.get("abstain"), bool):
            return False, f"role {name} 缺少 abstain 布尔字段"
        concerns = r.get("concerns")
        if not isinstance(concerns, list):
            return False, f"role {name} 的 concerns 必须是数组"
        if r["abstain"]:
            if concerns:
                return False, f"role {name} 弃权(abstain=true)却带 concern — 弃权必须空集"
            continue
        non_abstaining += 1
        for c in concerns:
            if not isinstance(c, dict):
                return False, f"role {name} 的 concern 含非 object 项"
            cid = c.get("id")
            if not isinstance(cid, str) or not cid.strip():
                return False, f"role {name} 的 concern 缺少非空 id"
            for fld in ("kind", "point"):
                v = c.get(fld)
                if not isinstance(v, str) or not v.strip():
                    return False, f"role {name} concern {cid} 缺少 {fld}"
            ev = c.get("evidence_ref")
            if not isinstance(ev, str) or not ev.strip():
                return False, f"role {name} concern {cid} 缺 evidence_ref — 疑似造假需求,禁止充数"
            role_concern_ids.add(cid)
    if non_abstaining == 0:
        return False, "全部角色弃权 — 未产出任何需求,不能定稿"

    union_ids = set()
    for u in gate["additive_union"]:
        if not isinstance(u, dict):
            return False, "additive_union 含非 object 项"
        uid = u.get("id")
        if not isinstance(uid, str) or not uid.strip():
            return False, "additive_union 项缺少非空 id"
        for fld in ("kind", "point"):
            v = u.get(fld)
            if not isinstance(v, str) or not v.strip():
                return False, f"additive_union 项 {uid} 缺少 {fld}"
        if not isinstance(u.get("sources"), list) or not u["sources"]:
            return False, f"additive_union 项 {uid} 缺少 sources"
        union_ids.add(uid)
    dropped = sorted(role_concern_ids - union_ids)
    if dropped:
        return False, "additive 并集漏掉角色关切(synthesizer 偷删,违反并集不减): " + ", ".join(dropped)

    open_forks = []
    for fk in gate["exclusive_forks"]:
        if not isinstance(fk, dict):
            return False, "exclusive_forks 含非 object 项"
        fid = fk.get("id")
        if not isinstance(fid, str) or not fid.strip():
            return False, "exclusive_forks 项缺少非空 id"
        if not isinstance(fk.get("decision"), str) or not fk["decision"].strip():
            return False, f"fork {fid} 缺少 decision"
        status = fk.get("status")
        if status not in ("open", "resolved"):
            return False, f"fork {fid} 的 status 必须是 open/resolved"
        if status == "open":
            open_forks.append(fid)
        elif not isinstance(fk.get("resolution"), str) or not fk["resolution"].strip():
            return False, f"fork {fid} 标 resolved 却无 resolution"
    if open_forks:
        return False, "存在未裁决 exclusive fork,需 grill 合岔路后定稿: " + ", ".join(open_forks)

    if not gate["p0"]:
        return False, "需求定稿缺少 P0 — feature 至少要有一个 P0 需求"
    for item in gate["p0"]:
        if not isinstance(item, dict):
            return False, "p0 含非 object 项"
        pid = item.get("id")
        if not isinstance(pid, str) or not pid.strip():
            return False, "p0 项缺少非空 id"
        if not isinstance(item.get("source"), str) or not item["source"].strip():
            return False, f"p0 {pid} 缺少 source(需求出处:用户原话/brief 证据/角色)"
        acs = item.get("observable_ac")
        if not isinstance(acs, list) or not acs:
            return False, f"p0 {pid} 缺少 observable_ac — 每个 P0 至少一条可观察 AC"
        for a in acs:
            if not isinstance(a, str) or not a.strip():
                return False, f"p0 {pid} 的 observable_ac 含空项"

    return True, "requirements 契约 PASS (synthesis 纪律 + 完备性已验证)"


def validate_requirements(orch: dict, hook: dict) -> tuple:
    """Step 1.3r gate — the 1A 需求定稿 (.fastship-requirements.md). Plumbing only:
    bind to the trusted artifact, extract the JSON contract, then delegate the
    discipline to the pure _check_requirements_contract (engine-derived verdict).
    """
    path = orch.get("artifacts", {}).get("requirements_path")
    if not path:
        return False, "requirements_path 未由当前 step 写入记录,禁止 filesystem fallback"
    ok, msg, _rec = _verify_step_artifact(orch, REQUIREMENTS_STEP_ID, path)
    if not ok:
        return False, msg
    if not os.path.isabs(path):
        path = os.path.join(_repo_root(), path)
    if REQUIREMENTS_FILENAME not in _normalize(path):
        return False, f"requirements 路径非法: {path}"
    try:
        content = open(path, encoding="utf-8").read()
    except Exception:
        return False, f"无法读取: {path}"
    if len(content) < 100:
        return False, f"requirements 太短 ({len(content)}B < 100B)"
    matches = CODEX_GATE_JSON_RE.findall(content)
    if not matches:
        return False, "requirements 缺少机器可验证 JSON 契约块"
    try:
        gate = json.loads(matches[-1])
    except json.JSONDecodeError as e:
        return False, f"requirements JSON 契约解析失败: {e}"
    return _check_requirements_contract(gate)


def validate_user_confirm(orch: dict, hook: dict) -> tuple:
    if orch.get("artifacts", {}).get("user_confirmed"):
        return True, "confirmed"
    return False, "等待用户确认"


def validate_execute(orch: dict, hook: dict) -> tuple:
    return True, "sequencing"


def validate_smoke(orch: dict, hook: dict) -> tuple:
    root = _repo_root()
    started_at = orch.get("started_at")
    try:
        result = subprocess.run(
            ["git", "-C", root, "diff", "--stat"],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip():
            return True, "uncommitted code changes detected"
        result2 = subprocess.run(
            ["git", "-C", root, "diff", "--cached", "--stat"],
            capture_output=True, text=True, timeout=10,
        )
        if result2.stdout.strip():
            return True, "staged code changes detected"
        if started_at:
            result3 = subprocess.run(
                ["git", "-C", root, "log", f"--since={started_at}", "--oneline"],
                capture_output=True, text=True, timeout=10,
            )
            if result3.stdout.strip():
                return True, "commits since session start detected"
    except Exception:
        pass
    return False, "No code changes detected — 执行阶段未产生代码变更"


def validate_tests(orch: dict, hook: dict) -> tuple:
    if hook.get("test_passed"):
        return True, f"tests passed ({hook.get('test_tool', '?')})"
    gate = _read_gate_state_file()
    if gate.get("test_passed"):
        return True, f"tests passed ({gate.get('test_tool', '?')}, via state file)"
    return False, "项目测试未通过"


def validate_e2e_run(orch: dict, hook: dict) -> tuple:
    if hook.get("e2e_executed"):
        return True, "e2e executed"
    gate = _read_gate_state_file()
    if gate.get("e2e_executed"):
        return True, "e2e executed (via state file)"
    if not gate:
        return False, "gate.json 不存在，禁止 Codex/filesystem fallback 通过 E2E Runner"
    return False, "E2E Runner 未执行"


def validate_e2e_report(orch: dict, hook: dict) -> tuple:
    gate = _read_gate_state_file()
    stored_hash = gate.get("e2e_result_hash") if gate else None
    result_path = _e2e_result_path()
    min_turns = _e2e_min_turns()

    if stored_hash:
        # Hook mode: verify e2e_result.json integrity
        if not os.path.exists(result_path):
            return False, f"{result_path} not found"
        with open(result_path, "rb") as f:
            actual_hash = hashlib.sha256(f.read()).hexdigest()
        if actual_hash != stored_hash:
            return False, "e2e_result.json hash mismatch — 文件在 runner 执行后被修改"
        try:
            with open(result_path, encoding="utf-8") as f:
                data = json.load(f)
            turns = sum(
                len(r.get("turns", []))
                for s in data.get("scenarios", [])
                for r in s.get("rounds", [])
            )
            if turns < min_turns:
                return False, f"e2e_result.json turns 不足 ({turns} < {min_turns})"
        except Exception as e:
            return False, f"e2e_result.json 解析失败: {e}"
        path = orch.get("report_path")
        if not path:
            return False, "report_path 未由当前 step 写入记录，禁止 filesystem fallback"
        ok, msg, _rec = _verify_step_artifact(orch, "3.3", path)
        if not ok:
            return False, msg
        if not os.path.exists(path):
            return False, "报告文件不存在"
        try:
            rsize = os.path.getsize(path)
        except OSError:
            rsize = 0
        if rsize < 200:
            return False, f"报告太短 ({rsize}B < 200B)"
        try:
            report_content = open(path, encoding="utf-8").read()
        except Exception:
            return False, f"无法读取报告: {path}"
        if stored_hash not in report_content:
            return False, "E2E 报告未引用 gate.json 中的 e2e_result_hash，禁止报告自证"
        return True, f"report verified (artifact + result hash match, {turns} turns)"

    if not gate:
        return False, "gate.json 不存在，禁止 Codex/filesystem fallback 通过 E2E 报告"

    # gate.json exists but no hash — e2e_runner wasn't run or hash not recorded
    return False, "gate.json 无 e2e_result_hash — E2E Runner 未正常执行"


def validate_e2e_gate(orch: dict, hook: dict) -> tuple:
    gate = _read_gate_state_file()
    result_path = _e2e_result_path()
    if not gate:
        return False, "gate.json 不存在，禁止 Codex fallback 通过 E2E Gate"
    if not gate.get("e2e_executed"):
        return False, "e2e_executed not set in gate.json"
    stored_hash = gate.get("e2e_result_hash")
    if stored_hash and os.path.exists(result_path):
        import hashlib
        with open(result_path, "rb") as f:
            actual_hash = hashlib.sha256(f.read()).hexdigest()
        if actual_hash != stored_hash:
            return False, "e2e_result.json hash mismatch — 文件在 3.3→3.4 间被修改"
    root = _repo_root()
    gate_script = None
    for candidate in ["tests/e2e_gate.py", ".claude/e2e/e2e_gate.py", "e2e/e2e_gate.py"]:
        path = os.path.join(root, candidate)
        if os.path.exists(path):
            gate_script = path
            break
    if not gate_script:
        return False, "e2e_gate.py not found in project"
    try:
        result = subprocess.run(
            [sys.executable, gate_script, "--result", result_path, "--min-turns", str(_e2e_min_turns())],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            output_tail = result.stdout[-500:] if result.stdout else ""
            return False, f"E2E Gate failed (exit {result.returncode}):\n{output_tail}"
        return True, "E2E Gate passed (subprocess verified, hash intact)"
    except subprocess.TimeoutExpired:
        return False, "E2E Gate timed out (30s)"
    except Exception as e:
        return False, f"E2E Gate execution error: {e}"


VALID_OUTCOMES = {"pass", "fail"}
VALID_DECISIONS = {"continue", "escalate", "stop"}


def validate_loop_record(orch: dict, hook: dict) -> tuple:
    outcome = orch.get("artifacts", {}).get("loop_outcome")
    if not outcome:
        return False, "未记录结果 (done --outcome pass|fail)"
    if outcome not in VALID_OUTCOMES:
        return False, f"无效 outcome: {outcome}。必须是 pass|fail"
    if outcome == "pass":
        gate = _read_gate_state_file()
        if not gate:
            return False, "gate.json 不存在，禁止 Codex fallback 自判 pass"
        if gate:
            if not gate.get("test_passed"):
                return False, "outcome=pass 但 gate.json test_passed=false — 测试未通过不能自判 pass"
            if not gate.get("e2e_executed"):
                return False, "outcome=pass 但 gate.json e2e_executed=false — E2E 未执行不能自判 pass"
            if not gate.get("e2e_gate_passed"):
                return False, "outcome=pass 但 gate.json e2e_gate_passed=false — E2E Gate 未通过不能自判 pass"
            stored_hash = gate.get("e2e_result_hash")
            result_path = _e2e_result_path()
            if stored_hash and os.path.exists(result_path):
                import hashlib
                with open(result_path, "rb") as f:
                    actual_hash = hashlib.sha256(f.read()).hexdigest()
                if actual_hash != stored_hash:
                    return False, "e2e_result.json hash mismatch — 文件在验证链中被篡改"
        return True, "pass (gate verified, hash intact)"
    decision = orch.get("artifacts", {}).get("loop_decision")
    if not decision:
        return False, "fail 但未给 decision (done --outcome fail --decision continue|escalate|stop)"
    if decision not in VALID_DECISIONS:
        return False, f"无效 decision: {decision}。必须是 continue|escalate|stop"
    return True, f"fail → {decision}"


def validate_knowledge(orch: dict, hook: dict) -> tuple:
    gate = _read_gate_state_file()
    acknowledged = hook.get("knowledge_acknowledged") or gate.get("knowledge_acknowledged")
    if acknowledged:
        if hook.get("knowledge_skip_reason") or gate.get("knowledge_skip_reason"):
            return True, "done (explicit skip via gate state)"
        path = (
            hook.get("knowledge_file")
            or gate.get("knowledge_file")
            or orch.get("artifacts", {}).get("knowledge_path")
        )
        if not path:
            return False, "knowledge_acknowledged 但缺少 knowledge_file provenance"
        ok, msg, _rec = _verify_step_artifact(orch, "3.6", path)
        if not ok:
            return False, msg
        return True, "done (trusted KNOWLEDGE artifact)"
    if not gate:
        return False, "gate.json 不存在，禁止 filesystem fallback 通过 KNOWLEDGE 闭环"
    return False, "KNOWLEDGE.md 未表态"


# ━━━━━━━━━━━━ Step Definitions ━━━━━━━━━━━━

STEPS = [
    Step("1.0", "需求分类", 1, validator=validate_classify,
         instruction="""分析用户需求，执行分类：
  python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" classify --type <bugfix|feature|refactor|optimize>

  bugfix = 报错/数据不对/线上问题    feature = 新功能
  refactor = 重构/规范               optimize = 性能/体验
  🔴 "报错/不对/403" = bugfix，不能降级。"""),

    Step("1.1", "上下文 + recall", 1, validator=validate_recall,
         instruction="""先读上下文，再跑 recall：
  1. Read ARCHITECTURE.md（Glob **/ARCHITECTURE.md → Read 全文）
  2. 确认 CLAUDE.md 已加载
  3. git log --oneline -15
  4. python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" knowledge_recall --query "<需求一句话>" --top 5

把 recall 命中原文保留，后续拷入 Brief。"""),

    Step("1.2", "并行 Explore", 1, validator=validate_explore,
         done_flags=["--agents"],
         instruction="""在单条消息里并行派 ≥3 个 Explore subagent：

  Agent A — 涉及模块清单（file_path:line、责任、入口、下游）
  Agent B — 现有测试/E2E 覆盖（file_path:line、覆盖范围、缺口）
  Agent C — 相关历史变更（最近 60 天 commit、已修 bug、TODO）

🔴 必须同一条消息发出多个 Agent 调用。主线程禁止亲自 grep/find。

完成后: "$(git rev-parse --show-toplevel)/.claude/tools/fastship" done --agents <N>"""),

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

    Step("1.3r", "1A 需求拷打", 1, validator=validate_requirements, conditional="non-bugfix",
         instruction="""1A 需求拷打（仅 non-bugfix，bugfix 自动跳过）：多角色法庭拷打需求 → 书记员合成 → grill 合岔路 → 需求定稿。

派 Workflow 并行多角色（产品/运营/数据/财务，吃 1.3 Brief 作输入）：
  - 各角色结构化输出 concern，每条带 evidence_ref（用户原话 / brief 的 file:line）；无实质关切→显式 abstain（合法，禁造假充数）
  - 书记员机械合成（书记员不是法官）：additive 取并集谁都不许删（标 sources）；exclusive 摊成 fork
  - grill 合 exclusive fork（无 open fork 自动跳过）

用 Write 写需求定稿到 .claude/.fastship-requirements.md，含 ```json 契约块（字段）：
  roles[]: role / abstain / concerns[]（每条 id,kind,point,evidence_ref）
  additive_union[]: id / kind / point / sources[]
  exclusive_forks[]: id / decision / status(open|resolved) / resolution
  p0[]: id / source / observable_ac[]    （p1 / constraints / open_questions 同理）

🔴 引擎硬验证(validate_requirements)：additive 并集不减 / fork 全 resolved / 每 P0 有 source+≥1 可观察 AC / concern 必带 evidence_ref。verdict 派生自结构，自报 PASS 无效。orchestrator 自动检测文件写入并验证。"""),

    Step("1.3d", "Bug 诊断", 1, validator=validate_diagnosis, conditional="bugfix",
         instruction="""Bugfix 诊断三步（缺一不可）：

  D1 复现: 实际跑出报错
    python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" bug_diagnosis reproduce --cmd '<命令>'

  D2 根因: 基于 D1 追踪到 file:line + 证据链
    python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" bug_diagnosis root_cause --cause '<根因>'

  D3 验证: 最小改动验证修复方向
    python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" bug_diagnosis fix_verified

🔴 禁止"读代码觉得会报错"，必须实际执行。"""),

    Step("1.4", "写计划", 1, validator=validate_plan,
         instruction="""通过 Skill 工具调用 superpowers 写计划：
  Skill(skill="writing-plans")

计划必须包含 AC 清单 + E2E 验证方案 + 影响范围 + 任务拆分。
🔴 为让自动生成的 plan.html 直观可视，请额外：
  - 加 `## 验收清单（AC）→ E2E 映射` 管道表（| AC | 可观察断言 | E2E scenario |），每条 AC 必有 E2E
  - 加 `## File Structure` 管道表（| File | Responsibility | Change |，Change ∈ Create/Modify/Test）
  - 加 `## 图示`：核心流程用 ```mermaid flowchart（ELK 布局自动更清晰）；
    模块/架构依赖图用 ```dot（Graphviz，`digraph{A->B}`，层级布局最清楚）
🔴 必须通过 Skill 工具调用，不要自己拆步骤。
产物: docs/superpowers/plans/YYYY-MM-DD-{feature}.md（plan 通过后自动生成同名 .plan.html）
orchestrator 自动检测 plan 文件写入 + 验证 writing-plans 签名。"""),

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

    Step("1.5c", "Codex Review", 1, validator=validate_codex_review,
         instruction=f"""调用 codex 对 plan 进行独立 review：
  Skill(skill="codex")
  → 选 review 模式，review 当前 plan 文件

Codex 输出后写结果到 .claude/{CODEX_REVIEW_FILENAME}：

  ## Codex Plan Review
  ### Findings
  - [P1/P2] {{finding}}
  ### Contract Gate
	  ```json
	  {{
	    "gate": "PASS",
	    "reviewed_plan_sha256": "<当前 1.4 plan artifact sha256>",
	    "p0_contract_reviewed": true,
    "ac_e2e_coverage_reviewed": true,
    "weak_case_reviewed": true,
    "evidence_plan_reviewed": true,
    "p0_requirements_missing": [],
    "uncovered_ac": [],
    "unmapped_e2e_scenarios": [],
    "weak_scenarios": [],
    "non_business_assertions": [],
    "missing_evidence": []
  }}
  ```
  ### GATE: PASS / FAIL

	Codex 必须按同一套 P0 contract / AC / E2E 证据规则审查：
	  - reviewed_plan_sha256 必须等于当前 1.4 plan artifact hash，禁止复用旧 review
	  - P0/P1 需求不能靠 agent 自己降级，缺 source/覆盖即 FAIL
  - 每个 P0/P1 AC 必须映射到 E2E scenario，未覆盖即 FAIL
  - 只测 button visible/page loads/status 200/no console error/text contains 的弱 case 必须列入 weak_scenarios 并 FAIL
  - 主断言必须验证业务结果或可观察证据，缺 screenshot/network/url/API/DB evidence 计划即 FAIL
🔴 纯文本 PASS 无效，JSON gate 任何问题数组非空或审查布尔项非 true 都不推进。
🔴 GATE: FAIL → 先更新 plan 修复 findings，再重新调 codex review。
   orchestrator 检测到 FAIL 自动回退到 1.4（写计划），更新后重走 1.5 → 1.5c。
🔴 GATE: PASS → 自动推进到用户确认。"""),

    Step("1.6", "用户确认", 1, validator=validate_user_confirm, done_flags=["--user-confirmed"],
         instruction="""向用户输出 AC + E2E + Plan 摘要，等待明确确认。
🔴 Phase 1 唯一确认关卡。

用户确认后: "$(git rev-parse --show-toplevel)/.claude/tools/fastship" done --user-confirmed"""),

    Step("2.0", "执行计划", 2, validator=validate_execute,
         instruction="""🎯 向用户展示 /goal 命令，进入自主执行模式（Phase 2+3 一气呵成）：
  运行: "$(git rev-parse --show-toplevel)/.claude/tools/fastship" goal
  将输出的 /goal 命令呈现给用户，请用户执行。

/goal 模式下用 dynamic workflow（ultracode）执行 plan —— 由你读 plan 自主决定扇出：
  1. 选择开发方式（worktree / 新分支 / 当前分支）。整个 implement 在【同一个 session worktree】里跑。
  2. 读已确认 plan 的 task 列表，做【依赖感知拆分】：
       - 文件【不相交】的 task → 可并行组；有先后依赖或改同一批文件的 → 串行链。
       - 不相交组【数量 ≥2】才用 Workflow parallel() 并行实现；只有一条链时直接
         subagent-driven 串行实现（不开 Workflow，省开销）。
       - 并行 agent 在【同一 worktree】内只编辑各自不相交的文件，【不各自 commit】
         （commit 由主线程逐组统一发，避免并行写 git index）。不开 per-agent worktree。
       - 并行 implement agent 只允许【编辑 + 编译检查】（cargo check / tsc）；
         【禁止跑项目测试套件 / E2E】——那是 step 3.1/3.2、主线程、串行干的
         （并行跑测试会撞 gate 状态写，语义上也错：要的是全部完成后跑全量）。
  3. implement→review pipeline：每个 task 实现完立刻被对抗性 review
       （设计稿保真度 / spec 合同 / 质量三视角），review 不过当场打回重做。
  4. 逐 task 的结构化 verdict（task / files_changed / 三视角结论）写入【session 绑定】的
       ledger：{git-dir}/fastship/sessions/<sid>/implement-verdicts.md
       （路径用 fastship_state.implement_verdicts_path() 解析；非门禁，作 2.5 的输入证据）。
       Step 2.5 读这个 ledger，合成 .claude/.fastship-code-review.md gate。
  5. 每步完成后运行 status，让 /goal 评估器看到 [FASTSHIP_GOAL] 进度。

🔴 一 session 一 worktree：多个并行需求 = 多个 git worktree。同一 worktree 内并行多 session 时，
   hook 会停止自动推进以防串台，须用 FASTSHIP_SESSION / use <session> 显式锁定。
🔴 禁止主线程凭直觉写代码；禁止并行 agent 改重叠文件或各自 commit。

执行完成 → done 进入 2.5 Code Review 合并 gate。
手动模式（不用 /goal）: "$(git rev-parse --show-toplevel)/.claude/tools/fastship" done"""),

    Step("2.5", "Code Review", 2, validator=validate_code_review,
         instruction="""🔴 Phase 2 硬 gate：对写出的代码做对抗性 code review（execute 阶段已逐 task review，这里合并成可审查产物 + 卡门）。

用 ultracode Workflow 跑多视角 review，对照三条 lens：
  1. 设计稿/spec 保真度 —— 逐屏/逐组件拿实现对着 plan 引用的设计依据（设计稿 HTML / spec / 截图）比对，
     列出所有偏差。tests 绿 ≠ 长得像设计稿。
  2. spec 合同 —— P0/P1 需求是否真的实现，有无被悄悄降级 / 漏做。
  3. 代码质量 —— 正确性、边界、与既有模式一致性。

把结果写到 .claude/.fastship-code-review.md，必须含：
  - 逐 task / 逐 lens 的 verdict
  - 一行 "### GATE: PASS" 或 "### GATE: FAIL"
  - 机器可验证 JSON gate（最后一个 json 代码块）字段：
      gate                    "PASS" | "FAIL"
      reviewed_against        plan 引用的设计稿/spec 路径（必须真实存在）
      reviewed_files          实际改动且审查过的文件列表（须与 git diff 相交）
      design_fidelity_reviewed  true
      spec_compliance_reviewed  true
      quality_reviewed          true
      design_deviations         []   ← 任一非空即 FAIL
      spec_gaps                 []
      quality_issues            []
      unverified_claims         []

🔴 任一 deviations/gaps/issues 数组非空 → GATE: FAIL → 修复实现后重新 review（留在 2.5，不回退 plan）。
🔴 reviewed_against 必须指向真实存在的设计依据；reviewed_files 必须与实际 git diff 相交（防橡皮图章）。

提交: "$(git rev-parse --show-toplevel)/.claude/tools/fastship" done --code-review .claude/.fastship-code-review.md"""),

    Step("3.0", "冒烟测试", 3, validator=validate_smoke,
         instruction="""零 setup 冒烟: 启动服务 → API 请求 → 等处理 → SELECT 验证。
🔴 禁止 DB 写入。失败 → 修，不进 E2E。
🔴 Validator 自动检测 git diff 中是否有代码变更。无变更 = 执行阶段未产出。

完成后: "$(git rev-parse --show-toplevel)/.claude/tools/fastship" done"""),

    Step("3.1", "项目测试", 3, validator=validate_tests,
         instruction="""运行项目全量测试。hook 自动检测通过。
失败 → 修复后重跑。orchestrator 自动检测 test pass。"""),

    Step("3.2", "E2E Runner", 3, validator=validate_e2e_run,
         instruction=_e2e_runner_instruction),

    Step("3.3", "E2E 报告", 3, validator=validate_e2e_report,
         instruction=_e2e_report_instruction),

    Step("3.4", "E2E Gate", 3, validator=validate_e2e_gate,
         instruction=_e2e_gate_instruction),

    Step("3.5", "Loop Record", 3, validator=validate_loop_record,
         instruction="""记录本轮结果：
  通过: python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" loop_record --outcome pass
  失败: 先写 reflection 到 docs/superpowers/plans/<plan>.reflections/loop-N.md
        然后: loop_record --outcome fail --reflection <path>

orchestrator 检测后需手动路由:
  "$(git rev-parse --show-toplevel)/.claude/tools/fastship" done --outcome pass
  "$(git rev-parse --show-toplevel)/.claude/tools/fastship" done --outcome fail --decision <continue|escalate|stop>

  continue → 回 3.1 重试    escalate → 回 1.0    stop → 停止"""),

    Step("3.6", "KNOWLEDGE 闭环", 3, validator=validate_knowledge,
         instruction="""merge 前表态：
  有教训 → 编辑 KNOWLEDGE.md（orchestrator 自动检测）
  无教训 → python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" knowledge_skip --reason "<≥10字>"
"""),
]

# Canonical ordered step-id list — the SINGLE SOURCE for any step-id consumer
# (forge dashboard/gate, tooling). STEPS above is where step ids are defined;
# consumers must derive from this constant (or be pinned to it by a guard test,
# e.g. tests/forge/test_step_ids_sync.py) rather than hardcoding their own copy.
ALL_STEP_IDS = [s.id for s in STEPS]


# ━━━━━━━━━━━━ Auto-Detection ━━━━━━━━━━━━

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
        exit_code = _extract_exit_code(data)
        if exit_code == 0:
            return "3.4"
        if exit_code is None:
            output = data.get("tool_response", {})
            stdout = output.get("stdout", "") if isinstance(output, dict) else ""
            if "GATE PASSED" in stdout:
                return "3.4"
        return None

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

    if current_step == "1.3r" and REQUIREMENTS_FILENAME in file_path:
        return "1.3r"

    if current_step == "1.4" and PLAN_DIR_MARKER in file_path and file_path.endswith(".md"):
        return "1.4"

    if current_step == "1.5" and GRILL_RESULT_FILENAME in file_path:
        return "1.5"

    if current_step == "1.5c" and CODEX_REVIEW_FILENAME in file_path:
        return "1.5c"

    if current_step == "2.5" and CODE_REVIEW_FILENAME in file_path:
        return "2.5"

    if current_step == "3.3" and file_path.endswith(".md"):
        if "e2e" in file_path.lower() or "report" in file_path.lower() or "质量" in file_path:
            return "3.3"

    if current_step == "3.6" and os.path.basename(file_path).upper() == "KNOWLEDGE.MD":
        return "3.6"

    return None


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
    p = _normalize(path)
    if BRIEF_FILENAME in p:
        return True
    if PLAN_DIR_MARKER in p:
        return True
    if os.path.basename(p).upper() == "KNOWLEDGE.MD":
        return True
    if ".reflections/" in p:
        return True
    if GRILL_RESULT_FILENAME in p:
        return True
    if CODEX_REVIEW_FILENAME in p:
        return True
    if CODE_REVIEW_FILENAME in p:
        return True
    if REQUIREMENTS_FILENAME in p:
        return True
    return False


def _extract_exit_code(data: dict) -> Optional[int]:
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


# ━━━━━━━━━━━━ Advance + Loop Logic ━━━━━━━━━━━━

REWINDABLE_STEPS = {"3.0", "3.1", "3.2", "3.3", "3.4", "3.5"}


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
        if candidate.conditional == "non-bugfix" and orch.get("request_type") == "bugfix":
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


def _handle_loop_decision(orch: dict):
    """Route based on decision after loop fail."""
    decision = orch.get("artifacts", {}).get("loop_decision")
    loop_count = orch.get("loop_count", 0)

    if loop_count >= 3:
        orch["current_step"] = "stopped"
        print(f"\n🔴 Loop 上限 ({loop_count}/3) — 流程停止。输出聚合分析给用户。")
        return

    if decision == "continue":
        # A loop fix MODIFIES the implementation, so the code-review gate (2.5) MUST
        # re-fire on the fixed code — otherwise design drift introduced while fixing a
        # verification failure ships unreviewed, which is the exact failure 2.5 exists
        # to prevent. Re-enter at 2.5 (not 3.1), clear it from completed_steps, and drop
        # its prior artifact so a fresh review of the now-current code is forced.
        cleared = REWINDABLE_STEPS | {"2.5"}
        orch["completed_steps"] = [s for s in orch.get("completed_steps", []) if s not in cleared]
        orch.setdefault("artifacts", {}).pop("code_review_path", None)
        _clear_trusted_artifacts(orch, ("2.5",))
        orch["current_step"] = "2.5"
        orch["phase"] = 2
        for k in ("loop_outcome", "loop_decision"):
            orch.get("artifacts", {}).pop(k, None)
        print(f"\n📝 Loop {loop_count} FAIL → continue → 回到 2.5 重新 code review + 重试验证")
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


# ━━━━━━━━━━━━ Hook Handlers (Logic) ━━━━━━━━━━━━

def hook_pre_edit_logic(data: dict, orch_state: Optional[dict],
                        gate_path: str, ambiguous: bool = False) -> int:
    file_path = data.get("tool_input", {}).get("file_path", "")

    if not orch_state:
        if os.path.exists(gate_path):
            code, stdout = delegate_to_gate(gate_path, "pre_edit", data)
            if stdout:
                print(stdout, end="")
            return code
        return 0

    # Session-INDEPENDENT block: editing fastship state files is always forbidden.
    normalized = _normalize(file_path)
    if (
        any(pat in normalized for pat in (
            "fastship/gate.json",
            "fastship/orchestrator.json",
            "fastship/registry.json",
            ".fastship-orchestrator-state.json",
            ".ship-verify-state.json",
        ))
        or ("fastship/sessions/" in normalized and normalized.endswith(("/gate.json", "/orchestrator.json")))
    ):
        print("🔴 BLOCKED: fastship state 由系统管理，禁止手动编辑")
        return 1

    # Fail-open under ambiguous multi-session: skip session-specific blocks.
    if ambiguous:
        print(_localize_cli(_AMBIGUOUS_HINT))
        return 0

    if _is_active(orch_state) and _branch_mismatch(orch_state):
        print("🔴 BLOCKED: Fastship branch mismatch")
        print(_branch_mismatch_text(orch_state))
        return 1

    # Block out-of-order step artifact writes
    if _is_active(orch_state):
        artifact_step = _artifact_owner_step(file_path)
        current = orch_state.get("current_step", "")
        if artifact_step and artifact_step != current:
            step_map = _get_step_map()
            owner = step_map.get(artifact_step)
            cur = step_map.get(current)
            print(f"🔴 BLOCKED: 当前在 step {current}"
                  f"{(' (' + cur.name + ')') if cur else ''}，"
                  f"不能写 step {artifact_step}"
                  f"{(' (' + owner.name + ')') if owner else ''} 的产物。")
            print(f"   必须按顺序完成当前步骤后才能产出下一步的文件。")
            if cur:
                print(f"\n📋 当前步骤指令:")
                print(f"{'─' * 50}")
                print(_localize_cli(cur.instruction))
            return 1

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
            lines.append(_localize_cli(current.instruction))
        print("\n".join(lines))
        return 1

    if os.path.exists(gate_path):
        code, stdout = delegate_to_gate(gate_path, "pre_edit", data)
        if stdout:
            print(stdout, end="")
        return code

    if _is_active(orch_state):
        print(f"🔴 BLOCKED: Fastship gate script unavailable: {gate_path}")
        print("   State is active, so edits are blocked instead of silently bypassing gates.")
        return 1

    return 0


def hook_pre_bash_logic(data: dict, orch_state: Optional[dict],
                        gate_path: str, ambiguous: bool = False) -> int:
    if not orch_state:
        if os.path.exists(gate_path):
            code, stdout = delegate_to_gate(gate_path, "pre_bash", data)
            if stdout:
                print(stdout, end="")
            return code
        return 0

    if ambiguous:
        print(_localize_cli(_AMBIGUOUS_HINT))
        return 0

    if _is_active(orch_state) and _branch_mismatch(orch_state):
        cmd = data.get("tool_input", {}).get("command", "")
        if not fastship_state.is_branch_recovery_command(cmd):
            print("🔴 BLOCKED: Fastship branch mismatch")
            print(_branch_mismatch_text(orch_state))
            return 1

    if os.path.exists(gate_path):
        code, stdout = delegate_to_gate(gate_path, "pre_bash", data)
        if stdout:
            print(stdout, end="")
        if code != 0:
            return code
    elif _is_active(orch_state):
        print(f"🔴 BLOCKED: Fastship gate script unavailable: {gate_path}")
        print("   State is active, so bash commands are blocked instead of silently bypassing gates.")
        return 1

    return 0


def _hook_session_ambiguous() -> bool:
    """True when ≥2 sessions are active in this state-home and none is pinned via
    FASTSHIP_SESSION — the editing context can't be mapped to one session."""
    if os.environ.get(fastship_state.SESSION_ENV):
        return False
    return len(fastship_state.active_session_ids()) >= 2


_AMBIGUOUS_HINT = (
    "⚠️ fastship: 检测到多个活跃 session 且未用 FASTSHIP_SESSION 锁定，为避免串台，"
    "本次 hook 不应用 session 专属逻辑。\n"
    "   并行需求请放各自 git worktree，或用 "
    "\"$(git rev-parse --show-toplevel)/.claude/tools/fastship\" use <session> 指定。"
)


def _other_active_sessions(current_sid: str) -> list:
    cur = fastship_state.normalize_session_id(current_sid)
    return [s for s in fastship_state.active_session_ids() if s != cur]


def _blocking_active_session_msg(current_sid: str):
    """Return a refusal message if another active session shares this
    state-home, else None."""
    others = _other_active_sessions(current_sid)
    if not others:
        return None
    return (
        f"🔴 本 worktree 已有活跃 session: {', '.join(others)}\n"
        f"   一 session 一 worktree 是默认隔离方式。请二选一：\n"
        f"     • 在新的 git worktree 里 start（推荐，隔离最干净）\n"
        f"     • 确需同 worktree 内并行：加 --shared 或 --session <id> 重新 start\n"
        f"   （同 worktree 多 session 时 hook 会停止自动推进以防串台，"
        f"且 .claude/.fastship-*.md 评审产物会共享。）"
    )


def hook_post_bash_logic(data: dict, orch_path: str = None,
                         hook_state: dict = None) -> int:
    if orch_path is None and _hook_session_ambiguous():
        print(_localize_cli(_AMBIGUOUS_HINT))
        return 0
    orch = load_orch_state(orch_path)
    if not orch or orch.get("current_step") in ("done", "stopped"):
        return 0

    hook = hook_state if hook_state is not None else load_hook_state()
    current = orch.get("current_step")

    detected = detect_completion_post_bash(current, data, hook)
    if detected:
        if detected == "1.0" and hook.get("request_type"):
            orch["request_type"] = hook["request_type"]

        if detected == "3.4":
            ok, msg = validate_e2e_gate(orch, hook)
            if not ok:
                print(f"⚠️ E2E Gate 命令已检测，但验证未通过: {msg}")
                save_orch_state(orch, orch_path)
                return 0

        if detected == "3.5":
            outcome = hook.get("last_loop_outcome")
            orch["loop_count"] = hook.get("loop_count", 0)
            if outcome == "pass":
                orch.setdefault("artifacts", {})["loop_outcome"] = "pass"
                ok, msg = validate_loop_record(orch, hook)
                if not ok:
                    print(f"⚠️ Loop Record pass 已检测，但验证未通过: {msg}")
                    save_orch_state(orch, orch_path)
                    return 0
            else:
                orch.setdefault("artifacts", {})["loop_outcome"] = "fail"
                save_orch_state(orch, orch_path)
                print(f"\n📝 Loop {orch['loop_count']} FAIL 已检测。需要手动指定路由：")
                print(_localize_cli('  "$(git rev-parse --show-toplevel)/.claude/tools/fastship" done \\'))
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
    if orch_path is None and _hook_session_ambiguous():
        print(_localize_cli(_AMBIGUOUS_HINT))
        return 0
    orch = load_orch_state(orch_path)
    if not orch or orch.get("current_step") in ("done", "stopped"):
        return 0

    current = orch.get("current_step")
    file_path = data.get("tool_input", {}).get("file_path", "")

    detected = detect_completion_post_edit(current, data)
    if detected:
        if detected == "1.3":
            orch["brief_path"] = file_path
            ok, msg = record_step_artifact(orch, "1.3", file_path)
            if not ok:
                print(f"⚠️ Brief artifact 记录失败: {msg}")
                save_orch_state(orch, orch_path)
                return 0
            hook = load_hook_state()
            ok, msg = validate_brief(orch, hook)
            if not ok:
                print(f"⚠️ Brief 写入已检测，但验证未通过: {msg}")
                save_orch_state(orch, orch_path)
                return 0

        if detected == "1.4":
            orch["plan_path"] = file_path
            ok, msg = record_step_artifact(orch, "1.4", file_path)
            if not ok:
                print(f"⚠️ Plan artifact 记录失败: {msg}")
                save_orch_state(orch, orch_path)
                return 0
            hook = load_hook_state()
            ok, msg = validate_plan(orch, hook)
            if not ok:
                print(f"⚠️ Plan 写入已检测，但验证未通过: {msg}")
                save_orch_state(orch, orch_path)
                return 0
            attach_plan_html(orch, file_path)
            for stale in (GRILL_RESULT_FILENAME, CODEX_REVIEW_FILENAME):
                p = os.path.join(_repo_root(), ".claude", stale)
                if os.path.exists(p):
                    os.remove(p)
            _clear_trusted_artifacts(orch, ("1.5", "1.5c"))

        if detected == "1.5":
            orch.setdefault("artifacts", {})["grill_result_path"] = file_path
            ok, msg = record_step_artifact(orch, "1.5", file_path)
            if not ok:
                print(f"⚠️ Grill artifact 记录失败: {msg}")
                save_orch_state(orch, orch_path)
                return 0
            hook = load_hook_state()
            ok, msg = validate_grill(orch, hook)
            if not ok:
                print(f"⚠️ Grill 结果写入已检测，但验证未通过: {msg}")
                save_orch_state(orch, orch_path)
                return 0

        if detected == "1.3r":
            orch.setdefault("artifacts", {})["requirements_path"] = file_path
            ok, msg = record_step_artifact(orch, "1.3r", file_path)
            if not ok:
                print(f"⚠️ 需求定稿 artifact 记录失败: {msg}")
                save_orch_state(orch, orch_path)
                return 0
            hook = load_hook_state()
            ok, msg = validate_requirements(orch, hook)
            if not ok:
                print(f"⚠️ 需求定稿写入已检测，但验证未通过: {msg}")
                save_orch_state(orch, orch_path)
                return 0

        if detected == "1.5c":
            orch.setdefault("artifacts", {})["codex_review_path"] = file_path
            ok, msg = record_step_artifact(orch, "1.5c", file_path)
            if not ok:
                print(f"⚠️ Codex review artifact 记录失败: {msg}")
                save_orch_state(orch, orch_path)
                return 0
            hook = load_hook_state()
            ok, msg = validate_codex_review(orch, hook)
            if not ok and "FAIL" in msg:
                review_path = os.path.join(_repo_root(), ".claude", CODEX_REVIEW_FILENAME)
                if os.path.exists(review_path):
                    os.remove(review_path)
                orch["current_step"] = "1.4"
                orch["phase"] = 1
                orch["plan_path"] = None
                orch.setdefault("artifacts", {}).pop("grill_result_path", None)
                orch.setdefault("artifacts", {}).pop("codex_review_path", None)
                _clear_trusted_artifacts(orch, ("1.4", "1.5", "1.5c"))
                for sid in ("1.4", "1.5", "1.5c"):
                    if sid in orch.get("completed_steps", []):
                        orch["completed_steps"].remove(sid)
                save_orch_state(orch, orch_path)
                print(f"\n🔄 Codex review FAIL — 回退到 1.4 更新 plan。")
                print(f"   {msg}")
                print(f"\n{format_next(orch)}")
                return 0
            if not ok:
                print(f"⚠️ Codex review 写入已检测，但验证未通过: {msg}")
                save_orch_state(orch, orch_path)
                return 0

        if detected == "2.5":
            orch.setdefault("artifacts", {})["code_review_path"] = file_path
            ok, msg = record_step_artifact(orch, "2.5", file_path)
            if not ok:
                print(f"⚠️ Code review artifact 记录失败: {msg}")
                save_orch_state(orch, orch_path)
                return 0
            hook = load_hook_state()
            ok, msg = validate_code_review(orch, hook)
            if not ok:
                print(f"⚠️ Code review 写入已检测，但验证未通过: {msg}")
                save_orch_state(orch, orch_path)
                return 0

        if detected == "3.3":
            orch["report_path"] = file_path
            ok, msg = record_step_artifact(orch, "3.3", file_path)
            if not ok:
                print(f"⚠️ 报告 artifact 记录失败: {msg}")
                save_orch_state(orch, orch_path)
                return 0
            hook = load_hook_state()
            ok, msg = validate_e2e_report(orch, hook)
            if not ok:
                print(f"⚠️ 报告写入已检测，但验证未通过: {msg}")
                save_orch_state(orch, orch_path)
                return 0

        if detected == "3.6":
            orch.setdefault("artifacts", {})["knowledge_path"] = file_path
            ok, msg = record_step_artifact(orch, "3.6", file_path)
            if not ok:
                print(f"⚠️ KNOWLEDGE artifact 记录失败: {msg}")
                save_orch_state(orch, orch_path)
                return 0
            hook = load_hook_state()
            ok, msg = validate_knowledge(orch, hook)
            if not ok:
                print(f"⚠️ KNOWLEDGE.md 写入已检测，但验证未通过: {msg}")
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


# ━━━━━━━━━━━━ Hook Entry Points (stdin) ━━━━━━━━━━━━

def hook_pre_edit():
    data = read_stdin()
    orch = load_orch_state()
    return hook_pre_edit_logic(data, orch, gate_script_path(),
                               ambiguous=_hook_session_ambiguous())


def hook_pre_bash():
    data = read_stdin()
    orch = load_orch_state()
    return hook_pre_bash_logic(data, orch, gate_script_path(),
                               ambiguous=_hook_session_ambiguous())


def hook_post_bash():
    data = read_stdin()
    gp = gate_script_path()
    if os.path.exists(gp):
        code, stdout = delegate_to_gate(gp, "post_bash", data)
        if stdout:
            print(stdout, end="")
    return hook_post_bash_logic(data)


def hook_post_edit():
    data = read_stdin()
    gp = gate_script_path()
    if os.path.exists(gp):
        code, stdout = delegate_to_gate(gp, "post_edit", data)
        if stdout:
            print(stdout, end="")
    return hook_post_edit_logic(data)


# ━━━━━━━━━━━━ CLI Arg Parsing ━━━━━━━━━━━━

VALUED_FLAGS = {
    "--agents",
    "--brief",
    "--requirements",
    "--plan",
    "--grill",
    "--codex-review",
    "--code-review",
    "--report",
    "--knowledge",
    "--outcome",
    "--decision",
}
BOOLEAN_FLAGS = {"--grill-complete", "--user-confirmed", "--shared"}


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
        f"   Session: {orch.get('session_id', fastship_state.current_session_id() or '-')}",
        f"   Phase: {orch.get('phase', '?')} | Step: {orch.get('current_step', '?')} | Loop: {orch.get('loop_count', 0)}/3",
        "",
    ]
    if _branch_mismatch(orch):
        lines.extend(fastship_state.branch_mismatch_lines(orch))
        lines.append("")
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

    gate = _read_gate_state_file()
    lines.append("")
    lines.append(
        f"[FASTSHIP_GOAL] step={cs} phase={orch.get('phase', '?')}"
        f" test_passed={str(gate.get('test_passed', False)).lower()}"
        f" e2e_executed={str(gate.get('e2e_executed', False)).lower()}"
        f" e2e_gate_passed={str(gate.get('e2e_gate_passed', False)).lower()}"
        f" code_reviewed={str('2.5' in orch.get('completed_steps', [])).lower()}"
        f" knowledge_acknowledged={str(gate.get('knowledge_acknowledged', False)).lower()}"
        f" loop={orch.get('loop_count', 0)}/3"
    )
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
    prefix = ""
    if _branch_mismatch(orch):
        prefix = "\n".join(fastship_state.branch_mismatch_lines(orch)) + "\n\n"

    instruction = step.instruction(orch) if callable(step.instruction) else step.instruction
    instruction = _localize_cli(instruction)

    return (
        prefix +
        f"📋 Step {step.id}: {step.name}  [{phase_names.get(step.phase, '?')}]\n"
        f"{'─' * 50}\n"
        f"{instruction}\n"
        f"{'─' * 50}"
    )


# ━━━━━━━━━━━━ CLI Commands ━━━━━━━━━━━━

def _session_id_for_start(requirement: str) -> str:
    explicit = fastship_state.normalize_session_id(os.environ.get(fastship_state.SESSION_ENV))
    if explicit:
        return explicit

    current = fastship_state.current_session_id()
    if current:
        current_orch = fastship_state.load_json(fastship_state.orchestrator_state_path(current)) or {}
        current_gate = fastship_state.load_json(fastship_state.gate_state_path(current)) or {}
        # Forge activates a feature by selecting its session before fastship starts.
        # Reuse that feature-scoped session instead of deriving a second id from
        # the natural-language requirement.
        if not _is_active(current_orch) and current_gate.get("forge_feature") == current:
            return current

    return fastship_state.session_id_from_requirement(requirement)


def cmd_start(requirement: str, argv: list = None) -> int:
    if argv is None:
        argv = []
    # Capture SESSION_ENV before we overwrite it below, to detect an explicit
    # --session <id> opt-in that was set by the global arg-stripping in main().
    explicit_session_before_start = os.environ.get(fastship_state.SESSION_ENV)
    session_id = _session_id_for_start(requirement)
    os.environ[fastship_state.SESSION_ENV] = session_id
    existing = load_orch_state(fastship_state.orchestrator_state_path(session_id))
    if existing and existing.get("current_step") not in ("done", "stopped", None):
        print(f"⚠️  session 已活跃: {session_id}")
        print(f"   需求: \"{existing.get('requirement')}\"")
        print(f"   当前: {existing.get('current_step')}")
        print(_localize_cli(f'   查看: "$(git rev-parse --show-toplevel)/.claude/tools/fastship" --session {session_id} status'))
        print(_localize_cli(f'   重来: "$(git rev-parse --show-toplevel)/.claude/tools/fastship" --session {session_id} reset'))
        return 1
    # Allow a second session only when the user explicitly opts in via --shared or
    # --session <id> (the latter is detected by SESSION_ENV being set before this
    # function ran, i.e. main() stripped and applied it).
    shared = "--shared" in argv or bool(explicit_session_before_start)
    if not shared:
        msg = _blocking_active_session_msg(session_id)
        if msg:
            print(msg)
            return 1
    if not _compact_is_recent():
        # Advisory, not a hard gate: a stale context is a quality risk, not a
        # correctness one — blocking start on it cost more than it saved. Warn and
        # proceed; the user decides whether to /compact first.
        print("🧠 SUGGESTION: 建议新 feature 前先 /compact，确保 context 干净。")
        print("   未检测到最近 2 分钟内 /compact；继续 start（不阻断）。")
    st = empty_orchestrator_state(requirement)
    st["session_id"] = session_id
    st["base_sha"] = _git_head_sha()
    fastship_state.set_current_session_id(session_id, requirement, st)
    save_orch_state(st)
    gp = gate_script_path()
    if os.path.exists(gp):
        delegate_to_gate(gp, "reset", {})
    print(f"🚀 Fastship started: \"{requirement}\"")
    print(f"   Session: {session_id}\n")
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
    if _branch_mismatch(st):
        print("🔴 Fastship flow is paused because the branch changed.")
        print(_branch_mismatch_text(st))
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

    for flag in step.done_flags:
        if flag not in args:
            print(f"❌ Step {step.id} 需要: {flag}")
            return 1

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
        ok, msg = record_step_artifact(st, "1.3", args["--brief"], source="cli_done")
        if not ok:
            print(f"❌ Brief artifact 记录失败: {msg}")
            return 1
    if "--requirements" in args:
        artifacts["requirements_path"] = args["--requirements"]
        ok, msg = record_step_artifact(st, "1.3r", args["--requirements"], source="cli_done")
        if not ok:
            print(f"❌ 需求定稿 artifact 记录失败: {msg}")
            return 1
    if "--plan" in args:
        st["plan_path"] = args["--plan"]
        ok, msg = record_step_artifact(st, "1.4", args["--plan"], source="cli_done")
        if not ok:
            print(f"❌ Plan artifact 记录失败: {msg}")
            return 1
    if "--grill" in args:
        artifacts["grill_result_path"] = args["--grill"]
        ok, msg = record_step_artifact(st, "1.5", args["--grill"], source="cli_done")
        if not ok:
            print(f"❌ Grill artifact 记录失败: {msg}")
            return 1
    if "--codex-review" in args:
        artifacts["codex_review_path"] = args["--codex-review"]
        ok, msg = record_step_artifact(st, "1.5c", args["--codex-review"], source="cli_done")
        if not ok:
            print(f"❌ Codex review artifact 记录失败: {msg}")
            return 1
    if "--code-review" in args:
        artifacts["code_review_path"] = args["--code-review"]
        ok, msg = record_step_artifact(st, "2.5", args["--code-review"], source="cli_done")
        if not ok:
            print(f"❌ Code review artifact 记录失败: {msg}")
            return 1
    if "--report" in args:
        st["report_path"] = args["--report"]
        ok, msg = record_step_artifact(st, "3.3", args["--report"], source="cli_done")
        if not ok:
            print(f"❌ 报告 artifact 记录失败: {msg}")
            return 1
    if "--knowledge" in args:
        artifacts["knowledge_path"] = args["--knowledge"]
        ok, msg = record_step_artifact(st, "3.6", args["--knowledge"], source="cli_done")
        if not ok:
            print(f"❌ KNOWLEDGE artifact 记录失败: {msg}")
            return 1
    if "--outcome" in args:
        artifacts["loop_outcome"] = args["--outcome"]
    if "--decision" in args:
        artifacts["loop_decision"] = args["--decision"]
    st["artifacts"] = artifacts

    hook = load_hook_state()

    # Sync request_type from gate state (classify CLI writes to gate state, not orchestrator)
    if step.id == "1.0":
        gate = _read_gate_state_file()
        rt = hook.get("request_type") or gate.get("request_type")
        if rt:
            st["request_type"] = rt

    ok, msg = step.validator(st, hook)
    if not ok:
        print(f"❌ Step {step.id} 验证失败: {msg}")
        save_orch_state(st)
        return 1

    # CLI parity with hook mode: render the plan visualization once 1.4 validates.
    if step.id == "1.4":
        attach_plan_html(st, st.get("plan_path"))

    # 3.5 loop fail → route by decision
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

    st = _advance_state(st)
    save_orch_state(st)
    next_step = step_map.get(st.get("current_step"))
    print(f"✅ Step {step.id} ({step.name}) 完成")
    if next_step:
        print()
        print(format_next(st))
        if step.id == "1.6" and st.get("current_step") == "2.0":
            _print_goal_hint(st)
    elif st.get("current_step") == "done":
        print("\n🎉 全部完成！")
    return 0


def cmd_status() -> int:
    st = load_orch_state()
    if not st:
        sessions = fastship_state.list_sessions()
        if sessions:
            print("❌ 当前没有选中的 session。可用 session:")
            print(format_session_list())
        else:
            print("❌ 没有活跃 session。")
        return 1
    print(format_status(st))
    return 0


def format_session_list() -> str:
    registry = fastship_state.load_registry()
    current = registry.get("current_session")
    sessions = registry.get("sessions", {})
    if not sessions:
        return "（无 fastship sessions）"
    lines = []
    for sid, rec in sorted(sessions.items()):
        marker = "*" if sid == current else " "
        req = rec.get("requirement", "-")
        step = rec.get("current_step", "-")
        status = rec.get("status", "-")
        branch = rec.get("branch", "-")
        lines.append(f"{marker} {sid}  step={step} status={status} branch={branch}  {req}")
    return "\n".join(lines)


def cmd_list() -> int:
    print(format_session_list())
    return 0


def cmd_use(session_id: str) -> int:
    sid = fastship_state.normalize_session_id(session_id)
    if not sid:
        print("Usage: use <session>")
        return 1
    registry = fastship_state.load_registry()
    if sid not in registry.get("sessions", {}):
        print(f"❌ Unknown fastship session: {sid}")
        print(format_session_list())
        return 1
    st = fastship_state.load_json(fastship_state.orchestrator_state_path(sid)) or {}
    fastship_state.set_current_session_id(sid, st.get("requirement"), st)
    print(f"✅ Current fastship session: {sid}")
    return 0


def cmd_reset(argv: list = None) -> int:
    argv = argv or []
    reset_all = "--all" in argv

    if reset_all:
        if os.path.exists(fastship_state.sessions_dir()):
            shutil.rmtree(fastship_state.sessions_dir())
        for path in (
            fastship_state.registry_path(),
            fastship_state.legacy_single_orchestrator_state_path(),
            fastship_state.legacy_single_gate_state_path(),
            fastship_state.legacy_orchestrator_state_path(),
            fastship_state.legacy_gate_state_path(),
        ):
            if os.path.exists(path):
                os.remove(path)
        print("✅ All Fastship sessions cleared.")
        return 0

    session_id = fastship_state.resolve_session_id(default=False)
    if not session_id:
        print("❌ 没有选中的 session。使用 list 查看，或 reset --all。")
        return 1

    for path in (
        fastship_state.orchestrator_state_path(session_id),
        fastship_state.gate_state_path(session_id),
    ):
        if os.path.exists(path):
            os.remove(path)
    session_dir = fastship_state.session_state_dir(session_id)
    if os.path.isdir(session_dir) and not os.listdir(session_dir):
        os.rmdir(session_dir)
    fastship_state.unregister_session(session_id)
    print(f"✅ Fastship session cleared: {session_id}")
    return 0


def cmd_render_plan(argv: list) -> int:
    """Render a plan.md to self-contained HTML on demand. Defaults to the active
    session's plan_path when no path is given."""
    plan_path = argv[0] if argv else None
    if not plan_path:
        st = load_orch_state()
        plan_path = (st or {}).get("plan_path")
    if not plan_path or not os.path.exists(plan_path):
        print("❌ 无 plan 文件。用法: render-plan <plan.md> 或在活跃 session 内 render-plan")
        return 1
    out = generate_plan_html(plan_path)
    if not out:
        return 1
    print(out)
    try:
        if _load_plan_html_mod().open_in_browser(out):
            print("   ↳ 已在浏览器打开（关闭：export FASTSHIP_PLAN_HTML_OPEN=never）")
    except Exception:  # noqa: BLE001 — opening is best-effort
        pass
    return 0


def goal_condition(orch: dict) -> str:
    """Generate a /goal condition string based on current orchestrator state."""
    req = orch.get("requirement", "?")
    return (
        f"fastship 完成「{req}」的交付 — "
        f"运行 status 命令确认 [FASTSHIP_GOAL] 显示 step=done"
        f" test_passed=true e2e_executed=true e2e_gate_passed=true code_reviewed=true knowledge_acknowledged=true"
    )


def _print_goal_hint(orch: dict):
    """Print /goal suggestion when entering Phase 2."""
    condition = goal_condition(orch)
    print()
    print("🎯 Plan 已确认，推荐使用 /goal 自主执行 Phase 2+3：")
    print(f"   /goal {condition}")
    print()


def cmd_goal() -> int:
    st = load_orch_state()
    if not st:
        print("❌ 没有活跃 session。")
        return 1
    phase = st.get("phase", 1)
    if phase < 2 and st.get("current_step") != "2.0":
        print("⚠️ 还在 Phase 1，完成 plan 确认后再用 /goal。")
        return 1
    condition = goal_condition(st)
    print(f"/goal {condition}")
    return 0


def cmd_adopt_branch() -> int:
    st = load_orch_state()
    if not st:
        print("❌ 没有活跃 session。")
        return 1
    current = _current_branch()
    if not current:
        print("❌ 当前目录无法识别 git branch，不能 adopt。")
        return 1

    old = st.get("branch")
    st["branch"] = current
    st["repo_root"] = _repo_root()
    save_orch_state(st)

    gate = load_hook_state()
    if gate:
        gate["branch"] = current
        save_hook_state(gate)

    print(f"✅ Fastship session adopted branch: {old or '-'} → {current}")
    return 0


# ━━━━━━━━━━━━ Main ━━━━━━━━━━━━

def strip_global_session_arg(argv: list[str]) -> tuple[Optional[str], list[str]]:
    session_id = None
    stripped = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--session", "-s") and i + 1 < len(argv):
            session_id = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--session="):
            session_id = arg.split("=", 1)[1]
            i += 1
            continue
        stripped.append(arg)
        i += 1
    return session_id, stripped


def main():
    session_arg, argv = strip_global_session_arg(sys.argv[1:])
    if session_arg:
        os.environ[fastship_state.SESSION_ENV] = fastship_state.normalize_session_id(session_arg) or session_arg

    if len(argv) < 1:
        print("Usage: fastship_orchestrator.py <command>")
        print()
        print("Hook mode (called by settings.local.json):")
        print("  pre_edit / pre_bash / post_edit / post_bash")
        print()
        print("CLI mode (called by Claude/Codex):")
        print("  start [--session ID] \"<需求>\"     开始/恢复需求 session")
        print("  next               当前步骤")
        print("  done [--flags]     完成当前步骤")
        print("  status             全部状态")
        print("  list               列出全部需求 sessions")
        print("  use <session>      切换 hook/CLI 默认 session")
        print("  goal               生成 /goal 条件（Phase 2+ 可用）")
        print("  adopt-branch       将活跃 session 迁移到当前分支")
        print("  reset [--all]      重置当前 session 或全部 sessions")
        sys.exit(1)

    cmd = argv[0]
    handlers = {
        "pre_edit": hook_pre_edit,
        "pre_bash": hook_pre_bash,
        "post_edit": hook_post_edit,
        "post_bash": hook_post_bash,
        "next": cmd_next,
        "status": cmd_status,
        "list": cmd_list,
        "goal": cmd_goal,
        "adopt-branch": cmd_adopt_branch,
    }

    if cmd == "start":
        if len(argv) < 2:
            print("Usage: start [--session ID] \"<需求>\"")
            sys.exit(1)
        sys.exit(cmd_start(argv[1], argv[2:]))
    elif cmd == "done":
        sys.exit(cmd_done(argv[1:]))
    elif cmd == "use":
        if len(argv) < 2:
            print("Usage: use <session>")
            sys.exit(1)
        sys.exit(cmd_use(argv[1]))
    elif cmd == "reset":
        sys.exit(cmd_reset(argv[1:]))
    elif cmd == "render-plan":
        sys.exit(cmd_render_plan(argv[1:]))
    elif cmd in handlers:
        sys.exit(handlers[cmd]())
    else:
        print(f"Unknown: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
