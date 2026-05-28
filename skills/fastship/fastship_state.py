#!/usr/bin/env python3
"""Shared state location helpers for fastship tools.

State is local runtime data. It should not live in tracked files, and it
should not disappear when the caller changes directories. The state home is:

1. FASTSHIP_STATE_HOME, when explicitly set.
2. Per-worktree: {git-dir}/fastship (supports parallel agents in worktrees).
3. The script repository's .claude/state/fastship directory as a fallback.
"""

import json
import os
import shutil
import subprocess
from typing import Optional


def _run_git(args: list[str], cwd: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _tools_dir() -> str:
    return os.path.dirname(os.path.realpath(__file__))


def _is_installed_tool_dir() -> bool:
    tools = _tools_dir()
    return (
        os.path.basename(tools) == "tools"
        and os.path.basename(os.path.dirname(tools)) == ".claude"
    )


def script_repo_root() -> str:
    return os.path.realpath(os.path.join(_tools_dir(), "..", ".."))


def repo_root() -> str:
    explicit = os.environ.get("FASTSHIP_REPO_ROOT")
    if explicit:
        return os.path.realpath(explicit)

    script_root = script_repo_root()
    script_git_root = _run_git(["rev-parse", "--show-toplevel"], script_root)

    if _is_installed_tool_dir():
        return os.path.realpath(script_git_root or script_root)

    cwd_root = _run_git(["rev-parse", "--show-toplevel"], os.getcwd())
    if cwd_root:
        return os.path.realpath(cwd_root)

    return os.path.realpath(script_git_root or script_root)


def git_common_dir() -> Optional[str]:
    root = repo_root()
    common = _run_git(["rev-parse", "--git-common-dir"], root)
    if not common:
        return None
    if not os.path.isabs(common):
        common = os.path.join(root, common)
    return os.path.realpath(common)


def git_dir() -> Optional[str]:
    """Per-worktree git dir (e.g. .git or .git/worktrees/<name>)."""
    root = repo_root()
    gd = _run_git(["rev-parse", "--git-dir"], root)
    if not gd:
        return None
    if not os.path.isabs(gd):
        gd = os.path.join(root, gd)
    return os.path.realpath(gd)


def state_home() -> str:
    explicit = os.environ.get("FASTSHIP_STATE_HOME")
    if explicit:
        return os.path.realpath(explicit)

    gd = git_dir()
    if gd:
        return os.path.join(gd, "fastship")

    return os.path.join(repo_root(), ".claude", "state", "fastship")


def ensure_state_home() -> str:
    home = state_home()
    os.makedirs(home, exist_ok=True)
    return home


def orchestrator_state_path() -> str:
    return os.path.join(ensure_state_home(), "orchestrator.json")


def gate_state_path() -> str:
    return os.path.join(ensure_state_home(), "gate.json")


def legacy_orchestrator_state_path() -> str:
    return os.path.join(script_repo_root(), ".claude", ".fastship-orchestrator-state.json")


def legacy_gate_state_path() -> str:
    return os.path.join(script_repo_root(), ".claude", ".ship-verify-state.json")


def gate_script_path() -> str:
    return os.path.join(script_repo_root(), ".claude", "hooks", "ship_verify_gate.py")


def fastship_cli_path() -> str:
    return os.path.join(script_repo_root(), ".claude", "tools", "fastship")


PROJECT_CONFIG_REL_PATH = os.path.join(".claude", "fastship.project.json")


def project_config_path() -> str:
    return os.path.join(repo_root(), PROJECT_CONFIG_REL_PATH)


def load_project_config() -> dict:
    data = load_json(project_config_path())
    return data if isinstance(data, dict) else {}


def current_branch() -> Optional[str]:
    return _run_git(["branch", "--show-current"], repo_root())


def load_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def migrate_legacy_state(kind: str) -> bool:
    if kind == "orchestrator":
        src = legacy_orchestrator_state_path()
        dst = orchestrator_state_path()
    elif kind == "gate":
        src = legacy_gate_state_path()
        dst = gate_state_path()
    else:
        raise ValueError(f"unknown state kind: {kind}")

    if os.path.exists(dst) or not os.path.exists(src):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True


def branch_mismatch(state: Optional[dict], current: Optional[str] = None) -> bool:
    if not state:
        return False
    saved = state.get("branch")
    current = current if current is not None else current_branch()
    return bool(saved and current and saved != current)


def branch_mismatch_lines(state: dict, tool_name: str = "Fastship") -> list[str]:
    saved = state.get("branch") or "-"
    current = current_branch() or "-"
    cli = fastship_cli_path()
    return [
        f"⚠️ {tool_name} session belongs to branch: {saved}",
        f"   Current branch: {current}",
        "",
        "   The flow is paused on this branch. Choose one:",
        f"     git switch {saved}",
        f"     \"{cli}\" adopt-branch",
        f"     \"{cli}\" reset",
    ]


def is_branch_recovery_command(command: str) -> bool:
    if not command:
        return False
    recovery_tokens = (
        "fastship_orchestrator.py status",
        "fastship_orchestrator.py adopt-branch",
        "fastship_orchestrator.py reset",
        "ship_verify_gate.py status",
        "ship_verify_gate.py reset",
        "git status",
        "git branch",
        "git switch",
        "git checkout",
    )
    return any(token in command for token in recovery_tokens)
