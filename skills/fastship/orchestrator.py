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
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable, Any, Union

import fastship_state


# ━━━━━━━━━━━━ Context Compact Gate ━━━━━━━━━━━━

COMPACT_RECENCY_SECS = int(os.environ.get("FASTSHIP_COMPACT_RECENCY", "120"))


def _last_compaction_epoch() -> float:
    log = os.path.join(_repo_root(), ".claude", "checkpoints", "compaction.log")
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256))
            last_line = f.read().decode().strip().rsplit("\n", 1)[-1]
            ts = last_line.split(" ", 1)[0]
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
    except Exception:
        return 0.0


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
E2E_RESULT_PATH = "/tmp/e2e_result.json"
E2E_MIN_TURNS = 10
GRILL_RESULT_FILENAME = ".fastship-grill-result.md"
CODEX_REVIEW_FILENAME = ".fastship-codex-review.md"

STEP_ARTIFACT_OWNERS = {
    BRIEF_FILENAME: "1.3",
    GRILL_RESULT_FILENAME: "1.5",
    CODEX_REVIEW_FILENAME: "1.5c",
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
🔴 必须通过 Skill 工具调用，不要自己拆步骤。
产物: docs/superpowers/plans/YYYY-MM-DD-{feature}.md
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

/goal 模式下 Claude 自主驱动：
  1. 选择开发方式（worktree / 新分支 / 当前分支）
  2. 通过 Skill 执行 plan（subagent-driven-development 或 executing-plans）
  3. 冒烟测试 → 项目测试 → E2E → 报告 → Gate → Loop Record → Knowledge 闭环
  4. 每步完成后运行 status 命令，让 /goal 评估器看到 [FASTSHIP_GOAL] 进度

🔴 禁止主线程凭直觉写代码。
🔴 每完成一个关键步骤后运行 status，确保 /goal 评估器能跟踪进度。

手动模式（不用 /goal）: "$(git rev-parse --show-toplevel)/.claude/tools/fastship" done"""),

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

    if current_step == "1.4" and PLAN_DIR_MARKER in file_path and file_path.endswith(".md"):
        return "1.4"

    if current_step == "1.5" and GRILL_RESULT_FILENAME in file_path:
        return "1.5"

    if current_step == "1.5c" and CODEX_REVIEW_FILENAME in file_path:
        return "1.5c"

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


# ━━━━━━━━━━━━ Hook Handlers (Logic) ━━━━━━━━━━━━

def hook_pre_edit_logic(data: dict, orch_state: Optional[dict],
                        gate_path: str) -> int:
    file_path = data.get("tool_input", {}).get("file_path", "")

    if not orch_state:
        if os.path.exists(gate_path):
            code, stdout = delegate_to_gate(gate_path, "pre_edit", data)
            if stdout:
                print(stdout, end="")
            return code
        return 0

    if _is_active(orch_state) and _branch_mismatch(orch_state):
        print("🔴 BLOCKED: Fastship branch mismatch")
        print(_branch_mismatch_text(orch_state))
        return 1

    # Block edits to any fastship state file
    normalized = _normalize(file_path)
    if any(pat in normalized for pat in ("fastship/gate.json", "fastship/orchestrator.json",
                                          ".fastship-orchestrator-state.json", ".ship-verify-state.json")):
        print("🔴 BLOCKED: fastship state 由系统管理，禁止手动编辑")
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
                print(cur.instruction)
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
            lines.append(current.instruction)
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
                        gate_path: str) -> int:
    if not orch_state:
        if os.path.exists(gate_path):
            code, stdout = delegate_to_gate(gate_path, "pre_bash", data)
            if stdout:
                print(stdout, end="")
            return code
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


def hook_post_bash_logic(data: dict, orch_path: str = None,
                         hook_state: dict = None) -> int:
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
                print('  "$(git rev-parse --show-toplevel)/.claude/tools/fastship" done \\')
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
    return hook_pre_edit_logic(data, orch, gate_script_path())


def hook_pre_bash():
    data = read_stdin()
    orch = load_orch_state()
    return hook_pre_bash_logic(data, orch, gate_script_path())


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
    "--plan",
    "--grill",
    "--codex-review",
    "--report",
    "--knowledge",
    "--outcome",
    "--decision",
}
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

    return (
        prefix +
        f"📋 Step {step.id}: {step.name}  [{phase_names.get(step.phase, '?')}]\n"
        f"{'─' * 50}\n"
        f"{instruction}\n"
        f"{'─' * 50}"
    )


# ━━━━━━━━━━━━ CLI Commands ━━━━━━━━━━━━

def cmd_start(requirement: str) -> int:
    existing = load_orch_state()
    if existing and existing.get("current_step") not in ("done", "stopped", None):
        print(f"⚠️  已有活跃 session: \"{existing.get('requirement')}\"")
        print(f"   当前: {existing.get('current_step')}")
        print('   重新开始: "$(git rev-parse --show-toplevel)/.claude/tools/fastship" reset')
        return 1
    if not _compact_is_recent():
        print("🧠 BLOCKED: 新 feature 前必须先 /compact，确保 context 干净。")
        print("   运行 /compact 后重试 start。")
        return 1
    st = empty_orchestrator_state(requirement)
    save_orch_state(st)
    gp = gate_script_path()
    if os.path.exists(gp):
        delegate_to_gate(gp, "reset", {})
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
        print("❌ 没有活跃 session。")
        return 1
    print(format_status(st))
    return 0


def cmd_reset() -> int:
    for path in (
        orch_state_path(),
        fastship_state.legacy_orchestrator_state_path(),
    ):
        if os.path.exists(path):
            os.remove(path)
    gp = gate_script_path()
    if os.path.exists(gp):
        delegate_to_gate(gp, "reset", {})
    print("✅ Orchestrator + hook state cleared.")
    return 0


def goal_condition(orch: dict) -> str:
    """Generate a /goal condition string based on current orchestrator state."""
    req = orch.get("requirement", "?")
    return (
        f"fastship 完成「{req}」的交付 — "
        f"运行 status 命令确认 [FASTSHIP_GOAL] 显示 step=done"
        f" test_passed=true e2e_executed=true e2e_gate_passed=true knowledge_acknowledged=true"
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

def main():
    if len(sys.argv) < 2:
        print("Usage: fastship_orchestrator.py <command>")
        print()
        print("Hook mode (called by settings.local.json):")
        print("  pre_edit / pre_bash / post_edit / post_bash")
        print()
        print("CLI mode (called by Claude/Codex):")
        print("  start \"<需求>\"     开始 session")
        print("  next               当前步骤")
        print("  done [--flags]     完成当前步骤")
        print("  status             全部状态")
        print("  goal               生成 /goal 条件（Phase 2+ 可用）")
        print("  adopt-branch       将活跃 session 迁移到当前分支")
        print("  reset              重置")
        sys.exit(1)

    cmd = sys.argv[1]
    handlers = {
        "pre_edit": hook_pre_edit,
        "pre_bash": hook_pre_bash,
        "post_edit": hook_post_edit,
        "post_bash": hook_post_bash,
        "next": cmd_next,
        "status": cmd_status,
        "goal": cmd_goal,
        "reset": cmd_reset,
        "adopt-branch": cmd_adopt_branch,
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
