#!/usr/bin/env python3
"""Shared state location helpers for fastship tools.

State is local runtime data. It should not live in tracked files, and it
should not disappear when the caller changes directories. The state home is:

1. FASTSHIP_STATE_HOME, when explicitly set.
2. Per-worktree: {git-dir}/fastship (supports parallel agents in worktrees).
3. The script repository's .claude/state/fastship directory as a fallback.

Within that home, runtime state is scoped by requirement/session:

  registry.json
  sessions/<session-id>/orchestrator.json
  sessions/<session-id>/gate.json

The registry only stores pointers and metadata. The actual flow state for one
requirement never shares a JSON document with another requirement.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime
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

    # Plugin-mode signal: when the engine runs as an installed Claude Code plugin,
    # CLAUDE_PROJECT_DIR points at the user's project root (the engine lives under
    # ~/.claude/plugins/cache/...). It wins over the installed-tool / cwd fallbacks
    # but stays below the explicit FASTSHIP_REPO_ROOT override above.
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir and os.path.isdir(project_dir):
        return os.path.realpath(project_dir)

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


def _now_iso() -> str:
    return datetime.now().isoformat()


def _status_from_state(state: dict) -> str:
    step = state.get("current_step")
    if step in ("done", "stopped"):
        return step
    if step:
        return "active"
    return "unknown"


REGISTRY_FILENAME = "registry.json"
SESSIONS_DIRNAME = "sessions"
DEFAULT_SESSION_ID = "default"
SESSION_ENV = "FASTSHIP_SESSION"


def normalize_session_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(os.sep, "-")
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip(".-_").lower()
    text = re.sub(r"-{2,}", "-", text)
    return (text or DEFAULT_SESSION_ID)[:96]


def session_id_from_requirement(requirement: str) -> str:
    base = normalize_session_id(requirement) or "req"
    digest = hashlib.sha1((requirement or "").encode("utf-8")).hexdigest()[:10]
    if base == DEFAULT_SESSION_ID:
        base = "req"
    max_base = 64 - len(digest) - 1
    return f"{base[:max_base].rstrip('-')}-{digest}"


def registry_path() -> str:
    return os.path.join(ensure_state_home(), REGISTRY_FILENAME)


def sessions_dir() -> str:
    return os.path.join(ensure_state_home(), SESSIONS_DIRNAME)


def load_registry() -> dict:
    data = load_json(registry_path())
    if not isinstance(data, dict):
        data = {}
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    return {
        "version": int(data.get("version", 1) or 1),
        "current_session": normalize_session_id(data.get("current_session")),
        "sessions": sessions,
    }


def save_registry(registry: dict) -> None:
    registry = dict(registry or {})
    registry["version"] = int(registry.get("version", 1) or 1)
    sessions = registry.get("sessions")
    registry["sessions"] = sessions if isinstance(sessions, dict) else {}
    registry["current_session"] = normalize_session_id(registry.get("current_session"))
    save_json(registry_path(), registry)


def current_session_id() -> Optional[str]:
    env_session = normalize_session_id(os.environ.get(SESSION_ENV))
    if env_session:
        return env_session
    current = load_registry().get("current_session")
    return normalize_session_id(current)


def list_sessions() -> dict:
    return load_registry().get("sessions", {})


def active_session_ids() -> list:
    """Session ids whose flow is still active (not done/stopped)."""
    out = []
    for sid, rec in (list_sessions() or {}).items():
        if (rec or {}).get("status") not in ("done", "stopped"):
            n = normalize_session_id(sid)
            if n:
                out.append(n)
    return sorted(out)


def set_current_session_id(session_id: str, requirement: str = None, state: dict = None) -> str:
    sid = normalize_session_id(session_id) or DEFAULT_SESSION_ID
    with state_lock():
        registry = load_registry()
        sessions = registry.setdefault("sessions", {})
        rec = dict(sessions.get(sid) or {})
        rec.update({
            "id": sid,
            "updated_at": _now_iso(),
            "repo_root": repo_root(),
        })
        if requirement:
            rec["requirement"] = requirement
        if state:
            rec["current_step"] = state.get("current_step")
            rec["phase"] = state.get("phase")
            rec["branch"] = state.get("branch")
            rec["status"] = _status_from_state(state)
            if state.get("requirement"):
                rec["requirement"] = state.get("requirement")
        rec.setdefault("created_at", rec["updated_at"])
        sessions[sid] = rec
        registry["current_session"] = sid
        save_registry(registry)
    return sid


def unregister_session(session_id: str) -> None:
    sid = normalize_session_id(session_id)
    if not sid:
        return
    with state_lock():
        registry = load_registry()
        registry.get("sessions", {}).pop(sid, None)
        if registry.get("current_session") == sid:
            remaining = sorted(registry.get("sessions", {}).keys())
            registry["current_session"] = remaining[0] if len(remaining) == 1 else None
        save_registry(registry)


def update_session_from_state(state: dict, session_id: str = None) -> None:
    if not isinstance(state, dict):
        return
    sid = normalize_session_id(session_id or state.get("session_id") or current_session_id())
    if not sid:
        return
    state["session_id"] = sid
    set_current_session_id(sid, state.get("requirement"), state)


def resolve_session_id(
    explicit: str = None,
    requirement: str = None,
    create: bool = False,
    default: bool = True,
) -> Optional[str]:
    sid = normalize_session_id(explicit)
    if sid:
        return sid

    env_session = normalize_session_id(os.environ.get(SESSION_ENV))
    if env_session:
        return env_session

    if create and requirement:
        return session_id_from_requirement(requirement)

    registry = load_registry()
    current = normalize_session_id(registry.get("current_session"))
    if current:
        return current

    sessions = registry.get("sessions", {})
    if isinstance(sessions, dict) and len(sessions) == 1:
        return normalize_session_id(next(iter(sessions.keys())))

    return DEFAULT_SESSION_ID if default else None


def session_state_dir(session_id: str = None) -> str:
    sid = resolve_session_id(explicit=session_id)
    return os.path.join(sessions_dir(), sid)


def orchestrator_state_path(session_id: str = None) -> str:
    return os.path.join(session_state_dir(session_id), "orchestrator.json")


def gate_state_path(session_id: str = None) -> str:
    return os.path.join(session_state_dir(session_id), "gate.json")


def implement_verdicts_path(session_id: str = None) -> str:
    return os.path.join(session_state_dir(session_id), "implement-verdicts.md")


def legacy_single_orchestrator_state_path() -> str:
    return os.path.join(ensure_state_home(), "orchestrator.json")


def legacy_single_gate_state_path() -> str:
    return os.path.join(ensure_state_home(), "gate.json")


def legacy_orchestrator_state_path() -> str:
    return os.path.join(script_repo_root(), ".claude", ".fastship-orchestrator-state.json")


def legacy_gate_state_path() -> str:
    return os.path.join(script_repo_root(), ".claude", ".ship-verify-state.json")


def gate_script_path() -> str:
    # ship_verify_gate.py is delegated by orchestrator via subprocess. Resolve it
    # relative to the engine's own location first — works for the source tree AND a
    # plugin install (both: <engine>/hooks/ship_verify_gate.py) — then fall back to
    # the legacy installed layout (.claude/hooks/ beside .claude/tools/).
    candidates = [
        os.path.join(_tools_dir(), "hooks", "ship_verify_gate.py"),
        os.path.join(script_repo_root(), ".claude", "hooks", "ship_verify_gate.py"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]


def orchestrator_script_path() -> str:
    # Resolve the orchestrator relative to the engine's own location so recovery
    # hints are correct in every layout: source/plugin keep the file as
    # orchestrator.py beside this module; the legacy installer renames it to
    # fastship_orchestrator.py under .claude/tools/.
    candidates = [
        os.path.join(_tools_dir(), "orchestrator.py"),
        os.path.join(_tools_dir(), "fastship_orchestrator.py"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]


PROJECT_CONFIG_REL_PATH = os.path.join(".claude", "fastship.project.json")


def project_config_path() -> str:
    return os.path.join(repo_root(), PROJECT_CONFIG_REL_PATH)


def load_project_config() -> dict:
    data = load_json(project_config_path())
    return data if isinstance(data, dict) else {}


def current_branch() -> Optional[str]:
    return _run_git(["branch", "--show-current"], repo_root())


_LOCAL = threading.local()


@contextlib.contextmanager
def state_lock():
    """Exclusive across processes (fcntl.flock on {state_home}/.lock), reentrant
    within a thread. Wrap registry/gate read-modify-write in this."""
    depth = getattr(_LOCAL, "depth", 0)
    if depth > 0:
        _LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _LOCAL.depth -= 1
        return

    home = ensure_state_home()
    f = open(os.path.join(home, ".lock"), "w")
    fcntl.flock(f, fcntl.LOCK_EX)
    _LOCAL.depth = 1
    _LOCAL.fd = f
    try:
        yield
    finally:
        _LOCAL.depth = 0
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()
            _LOCAL.fd = None


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
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def migrate_legacy_state(kind: str, session_id: str = None) -> bool:
    if kind == "orchestrator":
        sources = (legacy_single_orchestrator_state_path(), legacy_orchestrator_state_path())
        default_src = next((p for p in sources if os.path.exists(p)), None)
        default_data = load_json(default_src) if default_src else {}
        sid = (
            normalize_session_id(session_id)
            or normalize_session_id((default_data or {}).get("session_id"))
            or (session_id_from_requirement(default_data.get("requirement")) if default_data and default_data.get("requirement") else None)
            or resolve_session_id(default=False)
            or DEFAULT_SESSION_ID
        )
        dst = orchestrator_state_path(sid)
    elif kind == "gate":
        sources = (legacy_single_gate_state_path(), legacy_gate_state_path())
        sid = normalize_session_id(session_id) or resolve_session_id(default=False) or DEFAULT_SESSION_ID
        dst = gate_state_path(sid)
    else:
        raise ValueError(f"unknown state kind: {kind}")

    src = next((p for p in sources if os.path.exists(p)), None)
    if os.path.exists(dst) or not src:
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    data = load_json(dst) or {}
    data["session_id"] = sid
    save_json(dst, data)
    set_current_session_id(sid, data.get("requirement"), data)
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
    orch = orchestrator_script_path()
    return [
        f"⚠️ {tool_name} session belongs to branch: {saved}",
        f"   Current branch: {current}",
        "",
        "   The flow is paused on this branch. Choose one:",
        f"     git switch {saved}",
        f'     python3 "{orch}" adopt-branch',
        f'     python3 "{orch}" reset',
    ]


_ENGINE_SCRIPT_BASENAMES = frozenset({
    # source/plugin orchestrator, legacy installed orchestrator, legacy bash wrapper,
    # and the gate script — matched on the argv token's basename so substrings like
    # "not-orchestrator.py" never qualify.
    "orchestrator.py",
    "fastship_orchestrator.py",
    "fastship",
    "ship_verify_gate.py",
})
_RECOVERY_SUBCOMMANDS = frozenset({"status", "adopt-branch", "reset"})
_GIT_RECOVERY_SUBCOMMANDS = frozenset({"status", "branch", "switch", "checkout"})


# Shell metacharacters that can chain, redirect, background, or substitute extra
# commands. A recovery command must be a SINGLE simple invocation, so any of these in
# the raw string disqualifies it — scanning argv tokens is not enough because
# `git switch x && rm -rf /` or `python3 orch.py reset $(...)` would otherwise slip a
# second command past the branch-mismatch pause. Legitimate hints (git switch <branch>,
# python3 "<abspath>" <sub>, <wrapper> <sub>) contain none of these.
_SHELL_METACHARS = (";", "|", "&", "<", ">", "`", "$", "(", ")", "\n")
_ENV_PREFIX_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def is_branch_recovery_command(command: str) -> bool:
    if not command:
        return False
    raw = command.strip()
    if any(ch in raw for ch in _SHELL_METACHARS):
        return False
    try:
        # comments=True drops a trailing `# ...` so a recovery word in a comment
        # cannot whitelist a non-recovery run.
        tokens = shlex.split(raw, comments=True)
    except ValueError:
        return False
    # Strip leading `sudo` and NAME=value env-assignment prefixes — harmless, common.
    i = 0
    while i < len(tokens) and (tokens[i] == "sudo" or _ENV_PREFIX_RE.fullmatch(tokens[i])):
        i += 1
    rest = tokens[i:]
    if not rest:
        return False
    prog = os.path.basename(rest[0])
    # git escape hatch: `git <status|branch|switch|checkout> [args]`
    if prog == "git":
        return len(rest) >= 2 and rest[1] in _GIT_RECOVERY_SUBCOMMANDS
    # interpreter form: `python3 <engine-script> <status|adopt-branch|reset> [args]`
    if prog in ("python", "python3"):
        return (
            len(rest) >= 3
            and os.path.basename(rest[1]) in _ENGINE_SCRIPT_BASENAMES
            and rest[2] in _RECOVERY_SUBCOMMANDS
        )
    # direct wrapper form: `<engine-script> <status|adopt-branch|reset> [args]`
    if prog in _ENGINE_SCRIPT_BASENAMES:
        return len(rest) >= 2 and rest[1] in _RECOVERY_SUBCOMMANDS
    return False
