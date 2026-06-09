#!/usr/bin/env python3
"""
forge_gate.py — Project-level roadmap gate script

Manages the /forge state machine: roadmap I/O, feature state transitions,
gate validation, hook handlers, and roadmap.md generation.

Internal CLI (called by /forge skill, not by users directly):
  status                 — Print roadmap overview + overdue harvest reminders
  activate <slug>        — Set active feature
  transition <slug> <status> — Attempt state transition with gate check
  generate-view          — Regenerate roadmap.md from roadmap.json
  track <slug> <metric_id> <as_of> — Append a metric snapshot via metrics.project.json resolver
  analyze <slug>         — Direction-aware trend analysis over re-verified snapshots
  reset                  — Clear active feature state

Hook handlers (called by Claude Code hooks):
  pre_edit               — Protect derived forge state from tampering
  post_edit              — Detect metric.json/harvest.json/roadmap.json writes
  post_bash              — Check overdue harvests, print reminder

State files: {repo_root}/.claude/forge-state/features/<slug>/state.json (derived cache)
Source of truth: {repo_root}/project-roadmap/roadmap.json
Fastship trust inputs: current worktree {git-dir}/fastship/sessions/<slug>/gate.json + orchestrator.json.
Legacy single-file fastship state fallback is intentionally disabled.
"""

import sys
import os
import json
import re
import shlex
import subprocess
import hashlib
from datetime import datetime, timedelta
import time as _time


# ========== Context Compact Advisory ==========

COMPACT_RECENCY_SECS = int(os.environ.get("FORGE_COMPACT_RECENCY", "120"))


def _read_compaction_log_epoch(log_path: str) -> float:
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256))
            last_line = f.read().decode().strip().rsplit("\n", 1)[-1]
            ts = last_line.split(" ", 1)[0]
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
    except Exception:
        return 0.0


def _compaction_log_paths() -> list:
    """Candidate compaction.log locations, most-authoritative first.

    /compact's post-compact hook writes to the MAIN worktree's
    .claude/checkpoints/compaction.log (the parent of git-common-dir). Inside a
    linked worktree, get_repo_root() points at the worktree whose own log is
    stale, so the per-worktree path alone never sees the compact. Consult the
    shared (main-worktree) log as well and take the most recent across both —
    main-repo behaviour is unchanged since the two paths coincide there.
    Mirrors the fastship fix for the same worktree gate bug.
    """
    paths = []
    seen = set()

    def _add(p):
        if p and p not in seen:
            seen.add(p)
            paths.append(p)

    common = get_git_common_dir()
    if common:
        main_root = os.path.dirname(common)
        _add(os.path.join(main_root, ".claude", "checkpoints", "compaction.log"))

    root = get_repo_root() or "."
    _add(os.path.join(root, ".claude", "checkpoints", "compaction.log"))
    return paths


def _last_compaction_epoch() -> float:
    return max(
        (_read_compaction_log_epoch(p) for p in _compaction_log_paths()),
        default=0.0,
    )


def _compact_is_recent() -> bool:
    age = _time.time() - _last_compaction_epoch()
    return 0 <= age < COMPACT_RECENCY_SECS


def _activate_requires_compact() -> bool:
    """Repo policy knob: hard-block `/forge activate` when no recent /compact.

    Default False → advisory only (engine default). A consumer repo that wants
    the hard gate sets FORGE_ACTIVATE_REQUIRES_COMPACT=1|true|yes|on (e.g. in its
    .claude/settings.json hook env) — config, not a forked cmd_activate. Matches
    the FORGE_COMPACT_RECENCY env convention.
    """
    return os.environ.get("FORGE_ACTIVATE_REQUIRES_COMPACT", "").strip().lower() in (
        "1", "true", "yes", "on")


# ========== Helpers ==========

def get_repo_root():
    # Honor an explicit override / the plugin-mode project signal first, then fall
    # back to git-from-cwd. FORGE_REPO_ROOT mirrors fastship's FASTSHIP_REPO_ROOT;
    # CLAUDE_PROJECT_DIR is set by Claude Code when the engine runs as a plugin.
    explicit = os.environ.get("FORGE_REPO_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR")
    base = explicit if (explicit and os.path.isdir(explicit)) else None
    try:
        cmd = ["git", "rev-parse", "--show-toplevel"]
        if base:
            cmd = ["git", "-C", base, "rev-parse", "--show-toplevel"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return os.path.realpath(base) if base else None


def get_git_common_dir(root=None):
    root = root or get_repo_root()
    if not root:
        return None
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "--git-common-dir"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        common = r.stdout.strip()
        if not common:
            return None
        if not os.path.isabs(common):
            common = os.path.join(root, common)
        return os.path.realpath(common)
    except Exception:
        return None


def get_git_dir(root=None):
    """Per-worktree git dir (e.g. .git or .git/worktrees/<name>)."""
    root = root or get_repo_root()
    if not root:
        return None
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "--git-dir"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        gd = r.stdout.strip()
        if not gd:
            return None
        if not os.path.isabs(gd):
            gd = os.path.join(root, gd)
        return os.path.realpath(gd)
    except Exception:
        return None


def get_current_branch(root=None):
    root = root or get_repo_root()
    if not root:
        return None
    try:
        r = subprocess.run(["git", "-C", root, "branch", "--show-current"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


# ========== Worktree Cleanup (reaper) ==========

def _git_out(args, cwd=None):
    """Run git; return (returncode, stdout, stderr) as strings. Never raises.
    stderr is returned so callers can surface real git failure messages."""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)


def list_worktrees(root=None):
    """Parse `git worktree list --porcelain` into dicts:
    {path, head, branch, detached, is_main}. First entry is the main worktree."""
    root = root or get_repo_root()
    if not root:
        return []
    rc, out, _ = _git_out(["-C", root, "worktree", "list", "--porcelain"], cwd=root)
    if rc != 0:
        return []
    worktrees, cur = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur:
                worktrees.append(cur)
            cur = {"path": line[len("worktree "):], "head": None,
                   "branch": None, "detached": False, "is_main": False}
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            cur["branch"] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif line.strip() == "detached":
            cur["detached"] = True
    if cur:
        worktrees.append(cur)
    if worktrees:
        worktrees[0]["is_main"] = True
    return worktrees


def detect_trunk(root=None):
    """Trunk ref to test merges against: origin/main, main, origin/master,
    master — first that resolves. None if none exist."""
    root = root or get_repo_root()
    if not root:
        return None
    for ref in ("origin/main", "main", "origin/master", "master"):
        rc, _, _ = _git_out(["-C", root, "rev-parse", "--verify", "--quiet", ref], cwd=root)
        if rc == 0:
            return ref
    return None


def worktree_is_clean(path):
    """True iff no uncommitted or untracked changes in the worktree."""
    rc, out, _ = _git_out(["-C", path, "status", "--porcelain"], cwd=path)
    return rc == 0 and out.strip() == ""


def branch_merged(root, head_sha, trunk):
    """Conservative: True iff head_sha is an ancestor of trunk (a real merge).
    Squash-merges are intentionally NOT detected → returns False (kept safe)."""
    if not head_sha or not trunk:
        return False
    rc, _, _ = _git_out(["-C", root, "merge-base", "--is-ancestor", head_sha, trunk], cwd=root)
    return rc == 0


def managed_worktrees_root(root=None):
    root = root or get_repo_root()
    return os.path.realpath(os.path.join(root, ".claude", "worktrees")) if root else ""


def is_managed_worktree(wt_path, main_root=None):
    """True iff the worktree lives under <main_root>/.claude/worktrees/.
    `main_root` MUST be the MAIN worktree path (see classify_worktree)."""
    base = managed_worktrees_root(main_root)
    return bool(base) and os.path.realpath(wt_path).startswith(base + os.sep)


def classify_worktree(wt, main_root, trunk, current_path):
    """Decide whether one worktree may be reaped.
    `main_root` MUST be the MAIN worktree path (anchors the managed-scope check),
    NOT the current worktree — otherwise running from inside a linked worktree
    mis-derives the managed base and skips every sibling.
    Returns (removable: bool, status: str, reason: str)."""
    rpath = os.path.realpath(wt["path"])
    if rpath == os.path.realpath(current_path):
        return (False, "kept-current", "当前工作区，不能删除自身")
    if wt.get("is_main"):
        return (False, "kept-main", "主工作区")
    if not is_managed_worktree(rpath, main_root):
        return (False, "kept-unmanaged", "不在 .claude/worktrees/ 下，跳过")
    if not os.path.isdir(rpath):
        return (False, "kept-missing", "worktree 路径不存在")
    if wt.get("detached") or not wt.get("branch"):
        return (False, "kept-detached", "detached HEAD，无法判定合并")
    if not worktree_is_clean(rpath):
        return (False, "kept-dirty", "有未提交/未跟踪改动")
    if not branch_merged(main_root, wt.get("head"), trunk):
        return (False, "kept-unmerged", f"分支未并入 {trunk}（squash-merge 保守保留）")
    return (True, "removable", f"干净且已并入 {trunk}")


def remove_worktree(root, wt, delete_branch=True):
    """Remove a worktree WITHOUT --force (git refuses if dirty). Optionally
    delete its branch with -d (git refuses if not fully merged).
    PRECONDITION: callers MUST run classify_worktree() first — this function does
    NOT re-validate merged/clean/managed/current status. The only safety it adds on
    its own is git's refusal to remove a dirty worktree (no --force) or delete an
    unmerged branch (-d). All exposed paths (sweep_worktrees) classify before calling.
    Returns (ok, note): note carries a branch-not-deleted warning when applicable."""
    rc, _, err = _git_out(["-C", root, "worktree", "remove", wt["path"]], cwd=root)
    if rc != 0:
        return (False, f"git worktree remove 拒绝：{(err or '').strip()[:160]}")
    note = ""
    if delete_branch and wt.get("branch"):
        # -d is safe: git refuses to delete a not-fully-merged branch. If it
        # refuses (e.g. merged into trunk but not local HEAD), the worktree is
        # still gone; surface that the branch was kept instead of silently lying.
        brc, _, berr = _git_out(["-C", root, "branch", "-d", wt["branch"]], cwd=root)
        if brc != 0:
            note = f"（分支 {wt['branch']} 未删，已保留：{(berr or '').strip()[:80]}）"
    return (True, note)


def sweep_worktrees(root=None, dry_run=False, prune=False):
    """Reap ALL managed orphan worktrees (clean + truly merged into trunk).
    - prune=True: also run `git worktree prune` (clears admin entries whose working dir
      was manually deleted — safe, only acts on missing dirs, never loses committed work).
    Returns {removed:[(path,branch,reason)], kept:[(path,branch,status,reason)], trunk, error?}.

    Note: there is intentionally no per-feature targeting. git forbids removing the
    worktree you are standing in, so a per-feature reap run from inside `/forge ship`
    would always no-op; a full sweep instead reaps every other delivered feature's
    orphan and converges to zero across deliveries."""
    root = root or get_repo_root()           # current worktree (for never-remove-self)
    res = {"removed": [], "kept": [], "trunk": None}
    if not root:
        res["error"] = "不在 git 仓库中"
        return res
    wts = list_worktrees(root)
    if not wts:
        res["error"] = "无 worktree 列表"
        return res
    # Anchor the managed-scope check and all git ops on the MAIN worktree (always
    # the first `git worktree list` entry), NOT the current worktree — so a sweep
    # run from inside a linked feature worktree still sees siblings under
    # <main>/.claude/worktrees/ and reaps them.
    main_root = os.path.realpath(wts[0]["path"])
    trunk = detect_trunk(main_root)
    res["trunk"] = trunk
    if not trunk:
        res["error"] = "未找到 trunk (origin/main|main|origin/master|master)，跳过清理"
        return res
    for wt in wts:
        removable, status, reason = classify_worktree(wt, main_root, trunk, root)
        if not removable:
            res["kept"].append((wt["path"], wt.get("branch"), status, reason))
            continue
        if dry_run:
            res["removed"].append((wt["path"], wt.get("branch"), "DRY-RUN: 干净且已合并"))
            continue
        # Revalidate immediately before removal to close the classify→remove window
        # (defends against a concurrent commit / ref change between the two). git's
        # own guards (remove without --force, branch -d) already prevent data loss;
        # this makes the "never removes unmerged/dirty" invariant strict, not racy.
        rpath = os.path.realpath(wt["path"])
        # Re-read the worktree's CURRENT HEAD (not the stale one from list_worktrees):
        # a concurrent commit could leave the tree clean again while moving HEAD off
        # the merged SHA. Test merge against the fresh HEAD.
        #
        # An irreducible sub-ms window remains between this recheck and the remove
        # below (no cross-process lock exists for `git worktree remove`). It cannot
        # cause CODE LOSS: `git worktree remove` (no --force) refuses a dirty tree,
        # and `git branch -d` refuses an unmerged branch — so a raced commit keeps
        # its branch+commits even if the worktree directory (a recoverable checkout)
        # is removed. The contract is "never lose committed work", and that holds.
        rc_h, head_now, _ = _git_out(["-C", rpath, "rev-parse", "HEAD"], cwd=rpath)
        head_now = head_now.strip() if rc_h == 0 else wt.get("head")
        if not (worktree_is_clean(rpath) and branch_merged(main_root, head_now, trunk)):
            res["kept"].append((wt["path"], wt.get("branch"), "kept-raced", "判定后状态改变，保守保留"))
            continue
        ok, note = remove_worktree(main_root, wt)
        if ok:
            res["removed"].append((wt["path"], wt.get("branch"), reason + ((" " + note) if note else "")))
        else:
            res["kept"].append((wt["path"], wt.get("branch"), "kept-remove-failed", note))
    if prune and not dry_run:
        _git_out(["-C", main_root, "worktree", "prune"], cwd=main_root)
    return res


def removable_orphan_count(root=None):
    """Count managed worktrees that are clean + merged (safe to remove)."""
    root = root or get_repo_root()
    if not root:
        return 0
    wts = list_worktrees(root)
    if not wts:
        return 0
    main_root = os.path.realpath(wts[0]["path"])
    trunk = detect_trunk(main_root)
    if not trunk:
        return 0
    n = 0
    for wt in wts:
        removable, _, _ = classify_worktree(wt, main_root, trunk, root)
        if removable:
            n += 1
    return n


def _print_sweep(res):
    if res.get("error"):
        print(f"🧹 worktree 清理跳过：{res['error']}")
        return
    trunk = res.get("trunk")
    for path, branch, reason in res["removed"]:
        print(f"🧹 已清理 worktree: {path} [{branch}] — {reason}")
    for item in res["kept"]:
        path, branch, status = item[0], item[1], item[2]
        why = item[3] if len(item) > 3 else ""
        print(f"   保留 {path} [{branch}] — {status}{(': ' + why) if why else ''}")
    print(f"🧹 worktree sweep: 清理 {len(res['removed'])} 个，保留 {len(res['kept'])} 个 (trunk={trunk})")


def cmd_sweep_worktrees(dry_run=False):
    root = get_repo_root()
    if not root:
        print("❌ 不在 git 仓库中。")
        sys.exit(1)
    # Manual sweep targets all managed worktrees and also prunes orphan admin dirs.
    _print_sweep(sweep_worktrees(root, dry_run=dry_run, prune=True))


def roadmap_dir():
    root = get_repo_root()
    return os.path.join(root, "project-roadmap") if root else None


def roadmap_path():
    d = roadmap_dir()
    return os.path.join(d, "roadmap.json") if d else None


def _slug_id(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(os.sep, "-")
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip(".-_").lower()
    text = re.sub(r"-{2,}", "-", text)
    return text or None


def forge_state_home():
    root = get_repo_root()
    return os.path.join(root, ".claude", "forge-state") if root else None


def forge_registry_path():
    home = forge_state_home()
    return os.path.join(home, "registry.json") if home else None


def legacy_forge_state_path():
    root = get_repo_root()
    return os.path.join(root, ".claude", ".forge-state.json") if root else None


def _load_forge_registry():
    p = forge_registry_path()
    data = _load_json(p)
    features = data.get("features")
    if not isinstance(features, dict):
        features = {}
    return {
        "version": int(data.get("version", 1) or 1),
        "active_feature": _slug_id(data.get("active_feature")),
        "features": features,
    }


def _save_forge_registry(registry):
    p = forge_registry_path()
    if not p:
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    registry = dict(registry or {})
    registry["version"] = int(registry.get("version", 1) or 1)
    registry["active_feature"] = _slug_id(registry.get("active_feature"))
    if not isinstance(registry.get("features"), dict):
        registry["features"] = {}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)


def current_feature_id():
    env_feature = _slug_id(os.environ.get("FORGE_FEATURE"))
    if env_feature:
        return env_feature
    current = _load_forge_registry().get("active_feature")
    if current:
        return current
    legacy = _load_json(legacy_forge_state_path())
    return _slug_id(legacy.get("active_feature")) if legacy else None


def state_path(slug=None):
    home = forge_state_home()
    feature = _slug_id(slug) or current_feature_id()
    if not home or not feature:
        return None
    return os.path.join(home, "features", feature, "state.json")


def fastship_state_home(root=None):
    root = root or get_repo_root()
    if not root:
        return None
    explicit = os.environ.get("FASTSHIP_STATE_HOME")
    if explicit:
        return os.path.realpath(explicit)
    gd = get_git_dir(root)
    return os.path.join(gd, "fastship") if gd else None


def _load_fastship_registry():
    home = fastship_state_home()
    return _load_json(os.path.join(home, "registry.json")) if home else {}


def current_fastship_session_id():
    env_session = _slug_id(os.environ.get("FASTSHIP_SESSION"))
    if env_session:
        return env_session
    registry = _load_fastship_registry()
    current = _slug_id(registry.get("current_session"))
    if current:
        return current
    sessions = registry.get("sessions")
    if isinstance(sessions, dict) and len(sessions) == 1:
        return _slug_id(next(iter(sessions.keys())))
    return None


def fastship_session_dir(session_id=None):
    home = fastship_state_home()
    sid = _slug_id(session_id) or current_fastship_session_id()
    if not home or not sid:
        return None
    return os.path.join(home, "sessions", sid)


def fastship_state_path(session_id=None):
    d = fastship_session_dir(session_id)
    return os.path.join(d, "gate.json") if d else None


def fastship_orchestrator_state_path(session_id=None):
    d = fastship_session_dir(session_id)
    return os.path.join(d, "orchestrator.json") if d else None


def read_stdin():
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


# ========== Roadmap I/O ==========

def load_roadmap():
    p = roadmap_path()
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def save_roadmap(data):
    p = roadmap_path()
    if not p:
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def find_feature(roadmap, slug):
    for f in roadmap.get("features", []):
        if f.get("slug") == slug:
            return f
    return None


def _load_json(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _sha256_file(path):
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            size += len(chunk)
            h.update(chunk)
    return h.hexdigest(), size


def _resolve_feature_path(repo_root, slug, path):
    if not path:
        return None
    if os.path.isabs(path):
        return os.path.realpath(path)
    return os.path.realpath(os.path.join(repo_root, "project-roadmap", "features", slug, path))


# ========== Validation (Gates) ==========

METRIC_REQUIRED_FIELDS = ["metric_name", "event_name", "baseline", "target", "harvest_days", "data_source"]
HARVEST_REQUIRED_FIELDS = [
    "harvested_at",
    "actual",
    "baseline",
    "target",
    "verdict",
    "notes",
    "next_action",
    "evidence",
]
HARVEST_EVIDENCE_REQUIRED_FIELDS = ["source", "collected_at", "raw_path", "raw_sha256"]
VALID_VERDICTS = {"achieved", "partial", "missed"}
VALID_NEXT_ACTIONS = {"done", "iterate", "pivot"}
CODEX_GATE_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.IGNORECASE | re.DOTALL)
CODEX_GATE_RE = re.compile(r"#+\s*GATE:\s*(PASS|FAIL)\b", re.IGNORECASE)
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


def validate_metric(metric):
    """Validate metric.json structure (Gate 1). Returns (ok, errors)."""
    errors = []
    for field in METRIC_REQUIRED_FIELDS:
        if field not in metric or metric[field] is None or metric[field] == "":
            errors.append(f"Missing required field: {field}")
    if "baseline" in metric and metric["baseline"] is not None:
        if not isinstance(metric["baseline"], (int, float)):
            errors.append("baseline must be numeric")
    if "target" in metric and metric["target"] is not None:
        if not isinstance(metric["target"], (int, float)):
            errors.append("target must be numeric")
    if "harvest_days" in metric and metric["harvest_days"] is not None:
        if not isinstance(metric["harvest_days"], int) or metric["harvest_days"] < 1:
            errors.append("harvest_days must be a positive integer")
    return (len(errors) == 0, errors)


def validate_harvest(harvest):
    """Validate harvest.json structure (Gate 6). Returns (ok, errors)."""
    errors = []
    for field in HARVEST_REQUIRED_FIELDS:
        if field not in harvest or harvest[field] is None or harvest[field] == "":
            errors.append(f"Missing required field: {field}")
    if harvest.get("verdict") and harvest["verdict"] not in VALID_VERDICTS:
        errors.append(f"verdict must be one of: {VALID_VERDICTS}")
    if harvest.get("next_action") and harvest["next_action"] not in VALID_NEXT_ACTIONS:
        errors.append(f"next_action must be one of: {VALID_NEXT_ACTIONS}")
    if "actual" in harvest and harvest["actual"] is not None:
        if not isinstance(harvest["actual"], (int, float)):
            errors.append("actual must be numeric")
    evidence = harvest.get("evidence")
    if not isinstance(evidence, dict):
        errors.append("evidence must be an object")
    else:
        for field in HARVEST_EVIDENCE_REQUIRED_FIELDS:
            if field not in evidence or evidence[field] in (None, ""):
                errors.append(f"Missing required evidence field: {field}")
        raw_sha = evidence.get("raw_sha256")
        if raw_sha and not re.fullmatch(r"[a-fA-F0-9]{64}", str(raw_sha)):
            errors.append("evidence.raw_sha256 must be a SHA-256 hex digest")
    return (len(errors) == 0, errors)


def _trusted_artifact(orch_state, step_id):
    return (
        orch_state.get("artifacts", {})
        .get("trusted_artifacts", {})
        .get(step_id)
    )


def verify_trusted_artifact(orch_state, step_id):
    rec = _trusted_artifact(orch_state, step_id)
    if not rec:
        return (False, f"missing trusted artifact for step {step_id}", None)
    path = rec.get("path")
    if not path or not os.path.exists(path):
        return (False, f"trusted artifact path missing for step {step_id}", rec)
    if rec.get("step_id") != step_id:
        return (False, f"trusted artifact step mismatch for {step_id}", rec)
    try:
        digest, size = _sha256_file(path)
    except OSError as e:
        return (False, f"cannot hash trusted artifact for step {step_id}: {e}", rec)
    if digest != rec.get("sha256") or size != rec.get("size"):
        return (False, f"trusted artifact hash/size mismatch for step {step_id}", rec)
    return (True, "", rec)


def _plan_open_fork_ids(forks):
    """Pure: parse a plan's exclusive_forks → (open_ids, malformed). Mirrors the
    orchestrator's _check_exclusive_forks discipline: any bad shape (non-list/non-dict
    entry, blank/dup id, blank decision, bad status, resolved-without-resolution) sets
    malformed=True so callers fail CLOSED — the forge gate can't be fooled by a fork
    shape the engine would have rejected. Kept in lockstep by test_step_ids_sync.py."""
    if not isinstance(forks, list):
        return ([], True)
    seen = set()
    open_ids = []
    for fk in forks:
        if not isinstance(fk, dict):
            return ([], True)
        fid = fk.get("id")
        if not isinstance(fid, str) or not fid.strip() or fid in seen:
            return ([], True)
        seen.add(fid)
        if not isinstance(fk.get("decision"), str) or not fk["decision"].strip():
            return ([], True)
        status = fk.get("status")
        if status not in ("open", "resolved"):
            return ([], True)
        if status == "open":
            open_ids.append(fid)
        elif not isinstance(fk.get("resolution"), str) or not fk["resolution"].strip():
            return ([], True)
    return (open_ids, False)


def _forks_require_grill(forks):
    """Pure: does this plan's exclusive_forks list REQUIRE the 1.5 grill? True if any
    fork is OPEN **or** the list is MALFORMED (fails closed)."""
    open_ids, malformed = _plan_open_fork_ids(forks)
    return malformed or bool(open_ids)


def _extract_plan_open_fork_ids(plan_content):
    """(open_ids, malformed, has_block) from a trusted plan's ac_mapping json block."""
    gate = None
    for block in CODEX_GATE_JSON_RE.findall(plan_content):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "ac_mapping" in obj:
            gate = obj
    if not isinstance(gate, dict):
        return ([], True, False)
    open_ids, malformed = _plan_open_fork_ids(gate.get("exclusive_forks", []))
    return (open_ids, malformed, True)


def _check_grill_fork_resolution(open_fork_ids, grill_gate):
    """Pure: strictly mirror the orchestrator's _check_grill_fork_resolution VERDICT so
    Forge G2 rejects exactly the grill shapes the engine rejects — a non-object entry, a
    blank / dangling (not an open fork) / duplicate id, a blank resolution, or any open
    fork left unresolved. (Codex review [P2]: an earlier lenient `continue` let a grill
    the engine rejects pass G2.) Pinned in lockstep by tests/forge/test_step_ids_sync.py.
    Returns (ok, reason)."""
    if not isinstance(grill_gate, dict):
        return (False, "grill fork_resolutions gate not an object")
    res = grill_gate.get("fork_resolutions")
    if not isinstance(res, list) or not res:
        return (False, "grill fork_resolutions empty")
    resolved = set()
    for r in res:
        if not isinstance(r, dict):
            return (False, "fork_resolutions has a non-object entry")
        fid = r.get("id")
        if not isinstance(fid, str) or not fid.strip():
            return (False, "fork_resolutions entry missing id")
        if fid not in open_fork_ids:
            return (False, f"fork_resolutions resolves a non-open fork: {fid}")
        if fid in resolved:
            return (False, f"fork_resolutions duplicate id: {fid}")
        if not isinstance(r.get("resolution"), str) or not r["resolution"].strip():
            return (False, f"fork {fid} resolution empty")
        resolved.add(fid)
    missing = sorted(fid for fid in open_fork_ids if fid not in resolved)
    if missing:
        return (False, "open technical fork(s) not resolved in grill: " + ", ".join(missing))
    return (True, "")


def _grill_resolution_satisfied(orch_state):
    """For a non-bugfix whose 1.5 grill RAN (not legitimately skipped), re-derive the
    open technical forks from the SHA-verified 1.4 plan and require the SHA-verified 1.5
    grill summary to resolve EVERY one (a fork_resolutions entry with a non-empty
    resolution per open fork). Mirrors the orchestrator's validate_grill fork-resolution
    check so Forge G2 can't pass a feature whose open fork was never arbitrated — e.g. a
    grill recorded before this rule, or a forged state with prose-only grill. Returns
    (ok, reason). Fails CLOSED on any unreadable/malformed/missing evidence."""
    ok, reason, plan_rec = verify_trusted_artifact(orch_state, "1.4")
    if not ok:
        return (False, reason)
    ok, reason, grill_rec = verify_trusted_artifact(orch_state, "1.5")
    if not ok:
        return (False, reason)
    try:
        with open(plan_rec["path"], encoding="utf-8") as f:
            plan_content = f.read()
    except Exception as e:
        return (False, f"cannot read trusted plan artifact: {e}")
    open_ids, malformed, has_block = _extract_plan_open_fork_ids(plan_content)
    if not has_block:
        return (False, "trusted plan missing ac_mapping block (cannot verify fork resolution)")
    if malformed:
        return (False, "trusted plan exclusive_forks malformed")
    if not open_ids:
        return (True, "")  # no open fork → the grill had nothing to arbitrate
    try:
        with open(grill_rec["path"], encoding="utf-8") as f:
            grill_content = f.read()
    except Exception as e:
        return (False, f"cannot read trusted grill artifact: {e}")
    res_gate = None
    for block in CODEX_GATE_JSON_RE.findall(grill_content):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "fork_resolutions" in obj:
            res_gate = obj
    if not isinstance(res_gate, dict):
        return (False, "trusted grill missing fork_resolutions for open technical fork(s)")
    return _check_grill_fork_resolution(set(open_ids), res_gate)


def _trusted_plan_has_open_fork(orch_state):
    """Re-derive the open-fork decision from the SHA-verified 1.4 plan artifact, NOT
    from the mutable orch-state field. Returns (ok, requires_grill, reason). The phase-1
    gate honors a 1.5 grill skip only when the TRUSTED plan genuinely has no open
    exclusive fork — binding the waiver to trusted evidence (a forged/stale orch field
    or an edited plan can't move a fork-bearing feature to planned without arbitration).
    Fails CLOSED: if the trusted plan can't be read/parsed, or its forks are malformed,
    treat it as requiring the grill so the skip is NOT waived."""
    ok, reason, plan_rec = verify_trusted_artifact(orch_state, "1.4")
    if not ok:
        return (False, True, reason)
    try:
        with open(plan_rec["path"], encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return (False, True, f"cannot read trusted plan artifact: {e}")
    gate = None
    for block in CODEX_GATE_JSON_RE.findall(content):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "ac_mapping" in obj:
            gate = obj
    if not isinstance(gate, dict):
        # A validly-planned feature always carries the ac_mapping block; its absence is
        # anomalous → fail closed (don't waive the grill).
        return (True, True, "")
    return (True, _forks_require_grill(gate.get("exclusive_forks", [])), "")


def _extract_codex_review_gate(content):
    """Mirror of the orchestrator's _extract_codex_review_gate: the LAST contract-shaped
    json block (dict with gate ∈ PASS/FAIL + every CODEX_REVIEW_REQUIRED_EMPTY_FIELDS a
    list) appearing BEFORE the `### GATE:` text line AND whose `gate` equals the text
    verdict. Binding to the text verdict means a FAIL review with a trailing fake-PASS
    contract template selects the real FAIL gate (which verify then rejects), instead of
    the trailing PASS — closing the seam where Forge accepted a FAIL review as PASS.
    Pinned in lockstep with the engine by tests/forge/test_step_ids_sync.py."""
    if not content:
        return None
    m = CODEX_GATE_RE.search(content)
    if not m:
        return None
    verdict = m.group(1).upper()
    found = None
    for block in CODEX_GATE_JSON_RE.findall(content[:m.start()]):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if str(obj.get("gate", "")).upper() != verdict:
            continue
        if not all(isinstance(obj.get(f), list) for f in CODEX_REVIEW_REQUIRED_EMPTY_FIELDS):
            continue
        found = obj
    return found


def verify_codex_review_artifact(orch_state):
    ok, reason, codex_rec = verify_trusted_artifact(orch_state, "1.5c")
    if not ok:
        return (False, reason)
    ok, reason, plan_rec = verify_trusted_artifact(orch_state, "1.4")
    if not ok:
        return (False, reason)
    try:
        with open(codex_rec["path"], encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return (False, f"cannot read codex review artifact: {e}")
    gate = _extract_codex_review_gate(content)
    if gate is None:
        return (False, "Codex review missing structured contract JSON gate")
    if str(gate.get("gate", "")).upper() != "PASS":
        return (False, "Codex review JSON gate is not PASS")
    if gate.get("reviewed_plan_sha256") != plan_rec.get("sha256"):
        return (False, "Codex review not bound to current plan hash")
    missing_true = [field for field in CODEX_REVIEW_REQUIRED_TRUE_FIELDS if gate.get(field) is not True]
    if missing_true:
        return (False, "Codex review missing hard review fields: " + ", ".join(missing_true))
    missing_lists = [
        field for field in CODEX_REVIEW_REQUIRED_EMPTY_FIELDS
        if field not in gate or not isinstance(gate.get(field), list)
    ]
    if missing_lists:
        return (False, "Codex review missing issue arrays: " + ", ".join(missing_lists))
    unresolved = [field for field in CODEX_REVIEW_REQUIRED_EMPTY_FIELDS if gate.get(field)]
    if unresolved:
        return (False, "Codex review has unresolved issues: " + ", ".join(unresolved))
    return (True, "")


def fastship_phase1_complete(slug, fastship_state, orch_state):
    if not fastship_state.get("plan_ready"):
        return (False, "Gate 2: fastship plan not yet ready (plan_ready=false)")
    if not orch_state:
        return (False, "Gate 2: fastship orchestrator state missing")
    completed = set(orch_state.get("completed_steps", []))
    skipped = set(orch_state.get("skipped_steps", []))
    is_bugfix = orch_state.get("request_type") == "bugfix"
    # Only steps the engine could LEGITIMATELY skip in THIS request context count as
    # satisfied (and waive their artifact). Honoring every skipped_steps entry would
    # let a forged skip of a MANDATORY step bypass the gate: a bugfix must still run
    # its 1.5 grill, and a feature must still run its 1.3r 1A tribunal.
    allowed_skips = {"1.3r"} if is_bugfix else {"1.3d"}
    # F4: a feature may skip the 1.5 grill ONLY when its plan surfaced no open technical
    # fork — derived from the SHA-VERIFIED 1.4 plan artifact, not the mutable orch-state
    # field. If the trusted plan carries an open fork the engine routes to the grill
    # (mandatory human arbitration), so honoring a 1.5 skip there would bypass it.
    if not is_bugfix and "1.5" in skipped:
        ok, requires_grill, reason = _trusted_plan_has_open_fork(orch_state)
        if not ok:
            return (False, "Gate 2: " + reason)
        if not requires_grill:
            allowed_skips = allowed_skips | {"1.5"}
    honored_skips = skipped & allowed_skips
    satisfied = completed | honored_skips
    required = {"1.4", "1.5", "1.5c", "1.6"}
    artifact_steps = ["1.4", "1.5"]
    # 1A requirements tribunal (1.3r) is mandatory for non-bugfix features — without it
    # Forge could mark a feature "planned" with no 需求定稿. Unset/stale request_type
    # defaults to requiring 1A (safe: a feature missing its type must still have
    # produced requirements).
    if not is_bugfix:
        required = required | {"1.3r"}
        artifact_steps = ["1.3r"] + artifact_steps
    missing = sorted(required - satisfied)
    if missing:
        return (False, "Gate 2: fastship Phase 1 incomplete. Missing: " + ", ".join(missing))
    if not orch_state.get("artifacts", {}).get("user_confirmed"):
        return (False, "Gate 2: user confirmation missing")
    for step_id in artifact_steps:
        if step_id in honored_skips:
            continue  # legitimately-skipped step produced no artifact
        ok, reason, _ = verify_trusted_artifact(orch_state, step_id)
        if not ok:
            return (False, "Gate 2: " + reason)
    # When the 1.5 grill RAN (not legitimately skipped), the engine required it to have
    # resolved every open technical fork (item 2). Mirror that here so a trusted-but-
    # unresolving grill — recorded before this rule, or a forged state with a prose-only
    # grill — can't pass G2 with an open fork that was never arbitrated. (Same seam as
    # the F4 grill-skip waiver: the external gate must enforce the engine's discipline.)
    if not is_bugfix and "1.5" not in honored_skips:
        ok, reason = _grill_resolution_satisfied(orch_state)
        if not ok:
            return (False, "Gate 2: " + reason)
    ok, reason = verify_codex_review_artifact(orch_state)
    if not ok:
        return (False, "Gate 2: " + reason)
    return (True, "")


def fastship_phase3_complete(slug, fastship_state, orch_state):
    missing = []
    for field in ("test_passed", "e2e_executed", "e2e_gate_passed", "knowledge_acknowledged"):
        if not fastship_state.get(field):
            missing.append(field)
    if not fastship_state.get("e2e_result_hash"):
        missing.append("e2e_result_hash")
    if fastship_state.get("last_loop_outcome") != "pass":
        missing.append("last_loop_outcome=pass")
    if not isinstance(fastship_state.get("loop_count"), int) or fastship_state.get("loop_count", 0) < 1:
        missing.append("loop_count>=1")
    if fastship_state.get("e2e_runs_since_last_record", 0) != 0:
        missing.append("loop_record_after_latest_e2e")
    if missing:
        return (False, "Gate 4: fastship not complete. Missing: " + ", ".join(missing))
    if not orch_state:
        return (False, "Gate 4: fastship orchestrator state missing")
    if orch_state.get("current_step") != "done":
        return (False, f"Gate 4: fastship orchestrator not done (step={orch_state.get('current_step')})")
    ok, reason, report_rec = verify_trusted_artifact(orch_state, "3.3")
    if not ok:
        return (False, "Gate 4: " + reason)
    try:
        with open(report_rec["path"], encoding="utf-8") as f:
            report_content = f.read()
    except Exception as e:
        return (False, f"Gate 4: cannot read E2E report: {e}")
    if fastship_state["e2e_result_hash"] not in report_content:
        return (False, "Gate 4: E2E report is not bound to e2e_result_hash")
    if not fastship_state.get("knowledge_skip_reason"):
        ok, reason, _ = verify_trusted_artifact(orch_state, "3.6")
        if not ok:
            return (False, "Gate 4: " + reason)
    return (True, "")


# ========== State Machine ==========

VALID_STATUSES = ["draft", "planned", "in_progress", "shipped", "measuring", "concluded"]

TRANSITIONS = {
    ("draft", "planned"),
    ("planned", "in_progress"),
    ("in_progress", "shipped"),
    ("shipped", "measuring"),
    ("measuring", "concluded"),
}


def check_g1_metric(slug, repo_root):
    """Gate 1: metric.json must exist and be valid before entering draft."""
    if not repo_root:
        return (False, "Gate 1: cannot determine repo root")
    metric_path = os.path.join(repo_root, "project-roadmap", "features", slug, "metric.json")
    if not os.path.exists(metric_path):
        return (False, f"Gate 1: metric.json not found at {metric_path}")
    try:
        with open(metric_path) as f:
            metric = json.load(f)
        ok, errors = validate_metric(metric)
        if not ok:
            return (False, f"Gate 1: metric.json invalid — {'; '.join(errors)}")
    except Exception as e:
        return (False, f"Gate 1: cannot read metric.json — {e}")
    return (True, "")


def check_g6_harvest(slug, repo_root):
    """Gate 6: harvest.json must exist, be valid, and bind to raw evidence."""
    if not repo_root:
        return (False, "Gate 6: cannot determine repo root")
    feature_dir = os.path.join(repo_root, "project-roadmap", "features", slug)
    harvest_path = os.path.join(feature_dir, "harvest.json")
    if not os.path.exists(harvest_path):
        return (False, f"Gate 6: harvest.json not found at {harvest_path}")
    try:
        with open(harvest_path) as f:
            harvest = json.load(f)
        ok, errors = validate_harvest(harvest)
        if not ok:
            return (False, f"Gate 6: harvest.json invalid — {'; '.join(errors)}")
        evidence = harvest["evidence"]
        raw_path = _resolve_feature_path(repo_root, slug, evidence.get("raw_path"))
        if not raw_path or not os.path.exists(raw_path):
            return (False, f"Gate 6: evidence raw_path not found — {evidence.get('raw_path')}")
        feature_root = os.path.realpath(feature_dir)
        if not raw_path.startswith(feature_root + os.sep):
            return (False, "Gate 6: evidence raw_path must live under this feature directory")
        digest, size = _sha256_file(raw_path)
        if size <= 0:
            return (False, "Gate 6: evidence raw_path is empty")
        if digest != evidence.get("raw_sha256"):
            return (False, "Gate 6: evidence raw_sha256 mismatch")
    except Exception as e:
        return (False, f"Gate 6: cannot read harvest.json — {e}")
    return (True, "")


# ========== Metrics Tracking ==========

METRICS_PROJECT_REL = os.path.join(".claude", "metrics.project.json")
RESOLVER_PLACEHOLDERS = ("{metric_id}", "{as_of}", "{out}")
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]+$")  # 白名单：占位符值禁 shell 元字符


def _safe_owner_id(oid):  # owner id 进文件路径，比 token 更严：禁 ".."、"/"、前导点（堵 path traversal）
    return bool(oid) and ".." not in oid and "/" not in oid and not oid.startswith(".") and SAFE_TOKEN_RE.match(oid) is not None


def metrics_project_path():
    root = get_repo_root()
    return os.path.join(root, METRICS_PROJECT_REL) if root else None


def load_metrics_project_config():
    p = metrics_project_path()
    if not p or not os.path.exists(p):
        return (None, f"metrics.project.json not found at {p}")
    data = _load_json(p)
    cmd = data.get("resolver_command")
    if not isinstance(cmd, str) or not cmd.strip():
        return (None, "resolver_command missing/not a string")
    missing = [ph for ph in RESOLVER_PLACEHOLDERS if ph not in cmd]
    if missing:
        return (None, "resolver_command missing placeholders: " + ", ".join(missing))
    return (data, "")


def is_improvement(cur, prev, direction):
    return cur > prev if (direction or "up") == "up" else cur < prev


def metric_owner_dir(repo_root, kind, oid):
    return os.path.join(repo_root, "project-roadmap", kind, oid)


def metric_history_path(kind, oid):
    root = get_repo_root()
    return os.path.join(metric_owner_dir(root, kind, oid), "metric-history.jsonl") if root else None


def load_metric_history(kind, oid):
    p = metric_history_path(kind, oid)
    if not p or not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    return out


def _resolve_owner_raw(repo_root, kind, oid, raw_path):
    owner = os.path.realpath(metric_owner_dir(repo_root, kind, oid))
    raw = os.path.realpath(os.path.join(owner, raw_path))
    return (owner, raw)


def _verify_snapshot_evidence(repo_root, kind, oid, ev):
    for f in ("source", "collected_at", "raw_path", "raw_sha256"):
        if not ev.get(f):
            return (False, f"evidence.{f} missing")
    owner, raw = _resolve_owner_raw(repo_root, kind, oid, ev["raw_path"])
    if not raw.startswith(owner + os.sep) or not os.path.exists(raw):
        return (False, f"evidence raw_path bad: {ev.get('raw_path')}")
    digest, size = _sha256_file(raw)
    if size <= 0:
        return (False, "evidence raw_path empty")
    if digest != ev["raw_sha256"]:
        return (False, "evidence raw_sha256 mismatch")
    return (True, "")


def append_metric_snapshot(kind, oid, snap):
    root = get_repo_root()
    if not root:
        return (False, "no repo root")
    for f in ("metric_id", "as_of", "value", "baseline", "target", "direction", "evidence"):
        if snap.get(f) is None:
            return (False, f"snapshot.{f} missing (caller must enrich definition)")
    if not isinstance(snap["value"], (int, float)):
        return (False, "value must be numeric")
    ok, err = _verify_snapshot_evidence(root, kind, oid, snap["evidence"])
    if not ok:
        return (False, err)
    prev = load_metric_history(kind, oid)
    regression = bool(prev) and isinstance(prev[-1].get("value"), (int, float)) and not is_improvement(snap["value"], prev[-1]["value"], snap["direction"]) and snap["value"] != prev[-1]["value"]
    row = {"metric_id": snap["metric_id"], "as_of": snap["as_of"], "value": snap["value"],
           "baseline": snap["baseline"], "target": snap["target"], "direction": snap["direction"],
           "source": snap["evidence"]["source"], "raw_path": snap["evidence"]["raw_path"],
           "collected_at": snap["evidence"]["collected_at"], "evidence_sha256": snap["evidence"]["raw_sha256"],
           "regression": regression, "recorded_at": datetime.now().isoformat()}
    p = metric_history_path(kind, oid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return (True, "")


def verify_history_evidence(kind, oid):
    """Re-hash every row's raw_path vs stored evidence_sha256. (False, idx) on first mismatch."""
    root = get_repo_root()
    for i, row in enumerate(load_metric_history(kind, oid)):
        rp = row.get("raw_path")
        if not rp:
            return (False, f"row {i} missing raw_path")
        owner, raw = _resolve_owner_raw(root, kind, oid, rp)
        if not raw.startswith(owner + os.sep):
            return (False, f"row {i} raw_path escapes owner dir: {rp}")
        if not os.path.exists(raw):
            return (False, f"row {i} raw_path gone: {rp}")
        digest, _ = _sha256_file(raw)
        if digest != row.get("evidence_sha256"):
            return (False, f"row {i} evidence tampered: {rp}")
    return (True, "")


def _owner_metric_def(kind, oid, metric_id):
    """Curated definition: feature → metric.json; objective → roadmap objective.target_metric. Returns (def, err)."""
    root = get_repo_root()
    if kind == "features":
        mp = os.path.join(root, "project-roadmap", "features", oid, "metric.json")
        if not os.path.exists(mp):
            return (None, f"metric.json not found for feature {oid}")
        m = _load_json(mp)
        return ({"metric_id": m.get("metric_id") or metric_id, "baseline": m.get("baseline"),
                 "target": m.get("target"), "direction": m.get("direction", "up")}, "")
    rm = load_roadmap() or {}
    for obj in rm.get("objectives", []):
        if obj.get("id") == oid:
            tm = obj.get("target_metric")
            if isinstance(tm, dict):
                return ({"metric_id": tm.get("metric_id") or metric_id, "baseline": tm.get("baseline"),
                         "target": tm.get("target"), "direction": tm.get("direction", "up")}, "")
            return (None, f"objective {oid} target_metric not structured (legacy string)")
    return (None, f"objective {oid} not found")


def cmd_track(kind, oid, metric_id, as_of):
    if not (_safe_owner_id(oid) and SAFE_TOKEN_RE.match(metric_id) and SAFE_TOKEN_RE.match(as_of)):
        print("🚫 forge track: unsafe owner/metric_id/as_of (owner blocks '..' and '/'; tokens whitelist-only)")
        return 1
    cfg, err = load_metrics_project_config()
    if cfg is None:
        print(f"🚫 forge track: {err}")
        return 1
    mdef, e1 = _owner_metric_def(kind, oid, metric_id)
    if mdef is None:
        print(f"🚫 forge track: {e1}")
        return 1
    if mdef["metric_id"] and mdef["metric_id"] != metric_id:   # 防"取 X 记成 Y"
        print(f"🚫 forge track: CLI metric_id '{metric_id}' != curated '{mdef['metric_id']}'")
        return 1
    root = get_repo_root()
    out = os.path.join(metric_owner_dir(root, kind, oid), ".resolver-out.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    argv = [(metric_id if t == "{metric_id}" else as_of if t == "{as_of}" else out if t == "{out}" else t)
            for t in shlex.split(cfg["resolver_command"])]   # argv 替换，禁 shell=True
    try:
        r = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=120)
    except Exception as e:
        print(f"🚫 forge track: resolver run failed — {e}")
        return 1
    if r.returncode != 0:
        print(f"🚫 forge track: resolver exit {r.returncode} — {(r.stderr or '').strip()[:200]}")
        return 1
    out_data = _load_json(out)
    if not out_data or out_data.get("value") is None:
        print("🚫 forge track: resolver bad output")
        return 1
    snap = {**out_data, "baseline": mdef["baseline"], "target": mdef["target"], "direction": mdef["direction"],
            "metric_id": mdef["metric_id"]}   # enrich：定义来自 curate，不来自 resolver
    ok, e2 = append_metric_snapshot(kind, oid, snap)
    if not ok:
        print(f"🚫 forge track: snapshot rejected — {e2}")
        return 1
    last = load_metric_history(kind, oid)[-1]
    print(f"✅ forge track {kind}/{oid} {last['metric_id']}={last['value']} @ {last['as_of']}" + ("  ⚠️ regression" if last['regression'] else ""))
    return 0


ALIGN_ON_TRACK = 80.0
ALIGN_AT_RISK = 40.0


def _target_metric_struct(obj):
    tm = obj.get("target_metric")
    return tm if isinstance(tm, dict) else None


def compute_objective_alignment(obj):
    tm = _target_metric_struct(obj)
    if not tm:
        return None  # 旧 string → 不渲染对齐（向后兼容）
    hist = load_metric_history("objectives", obj.get("id", ""))
    cur = hist[-1]["value"] if hist else None
    as_of = hist[-1]["as_of"] if hist else None
    base, target = tm.get("baseline"), tm.get("target")
    pct, status = None, "no_data"
    if cur is not None and isinstance(base, (int, float)) and isinstance(target, (int, float)) and target != base:
        pct = round((cur - base) / (target - base) * 100.0, 1)  # 方向无关（base/target 编码方向）
        status = "on_track" if pct >= ALIGN_ON_TRACK else ("at_risk" if pct >= ALIGN_AT_RISK else "off_track")
    return {"metric_id": tm.get("metric_id"), "baseline": base, "target": target, "current": cur, "pct": pct, "status": status, "as_of": as_of}


def _linear_slope(vals):
    n = len(vals)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(vals) / n
    den = sum((x - mx) ** 2 for x in xs)
    return 0.0 if den == 0 else round(sum((xs[i] - mx) * (vals[i] - my) for i in range(n)) / den, 4)


def cmd_analyze(kind, oid):
    if not _safe_owner_id(oid):
        print("🚫 forge analyze: unsafe owner id (blocks '..' and '/')")
        return 1
    ok, why = verify_history_evidence(kind, oid)          # F6：先复验，篡改即拒
    if not ok:
        print(f"🚫 forge analyze: history evidence check failed — {why}")
        return 1
    root = get_repo_root()
    hist = load_metric_history(kind, oid)
    if not hist:
        print(f"🚫 forge analyze: no history for {kind}/{oid}")
        return 1
    vals = [h["value"] for h in hist if isinstance(h.get("value"), (int, float))]
    last = hist[-1]
    slope = _linear_slope(vals)
    trend = "up" if slope > 0 else ("down" if slope < 0 else "flat")
    target = last.get("target")
    direction = last.get("direction", "up")
    projected = None
    if isinstance(target, (int, float)):
        if not is_improvement(target, vals[-1], direction):   # latest 已达/越过 target（含相等）
            projected = {"periods_to_target": 0, "reachable": True, "achieved": True}
        elif slope != 0:
            periods = round((target - vals[-1]) / slope, 1)
            reachable = is_improvement(vals[-1] + slope, vals[-1], direction)  # 朝改善方向移动即可达
            projected = {"periods_to_target": periods, "reachable": reachable, "achieved": False}
    analysis = {"owner": f"{kind}/{oid}", "metric_id": last.get("metric_id"), "samples": len(vals),
                "first": vals[0], "latest": vals[-1], "baseline": last.get("baseline"), "target": target,
                "direction": direction, "slope": slope, "trend": trend, "regressions": sum(1 for h in hist if h.get("regression")),
                "projected": projected,
                "provenance": {"source_tier": last.get("source") or "resolver", "as_of": last.get("as_of"),
                               "owner": f"{kind}/{oid}", "raw_path": last.get("raw_path"), "evidence_sha256": last.get("evidence_sha256"),
                               "note": "deterministic trend over re-verified evidence-bound snapshots; adversarial attribution via skills/forge/workflows/analyze.workflow.js"}}
    out = os.path.join(metric_owner_dir(root, kind, oid), "analysis.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2, ensure_ascii=False)
    print(f"✅ forge analyze {kind}/{oid}: {trend} (slope={slope}, {vals[-1]}/{target}) → {out}")
    return 0


def can_transition(slug, current_status, target_status, repo_root, fastship_state=None, fastship_orch_state=None):
    """Check if a state transition is allowed. Returns (ok, reason)."""
    if (current_status, target_status) not in TRANSITIONS:
        return (False, f"Not allowed: {current_status} → {target_status}. "
                       f"Valid transitions from {current_status}: "
                       f"{[t for (s, t) in TRANSITIONS if s == current_status]}")

    if fastship_state is None:
        fastship_state = load_fastship_state(slug)
    if fastship_orch_state is None:
        fastship_orch_state = load_fastship_orchestrator_state(slug)

    # Verify fastship state belongs to this feature
    fs_feature = fastship_state.get("forge_feature")
    needs_fs_check = (
        (current_status == "draft" and target_status == "planned") or
        (current_status == "in_progress" and target_status == "shipped")
    )
    if needs_fs_check:
        if not fs_feature:
            return (False, f"Fastship state not bound to any feature. "
                           f"Run: forge_gate.py activate {slug}")
        if fs_feature != slug:
            return (False, f"Fastship state belongs to feature '{fs_feature}', not '{slug}'. "
                           f"Run: forge_gate.py activate {slug}")

    # Gate 2: draft → planned (fastship Phase 1 complete)
    if current_status == "draft" and target_status == "planned":
        ok, reason = fastship_phase1_complete(slug, fastship_state, fastship_orch_state)
        if not ok:
            return (False, reason)

    # Gate 4: in_progress → shipped (fastship Phase 3 complete)
    if current_status == "in_progress" and target_status == "shipped":
        ok, reason = fastship_phase3_complete(slug, fastship_state, fastship_orch_state)
        if not ok:
            return (False, reason)

    # Gate 6: measuring → concluded (harvest.json must exist and be valid)
    if current_status == "measuring" and target_status == "concluded":
        ok, reason = check_g6_harvest(slug, repo_root)
        if not ok:
            return (False, reason)

    return (True, "")


# ========== Overdue Harvest Detection ==========

def get_overdue_harvests(features, today_str=None):
    """Return list of features in measuring status past harvest_due date."""
    if today_str is None:
        today_str = datetime.now().strftime("%Y-%m-%d")
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    overdue = []
    for f in features:
        if f.get("status") != "measuring":
            continue
        due = f.get("harvest_due")
        if due:
            due_date = datetime.strptime(due, "%Y-%m-%d").date()
            if today >= due_date:
                overdue.append(f)
    return overdue


# ========== State Derivation ==========

def load_fastship_state(session_id=None):
    p = fastship_state_path(session_id)
    return _load_json(p)


def load_fastship_orchestrator_state(session_id=None):
    p = fastship_orchestrator_state_path(session_id)
    return _load_json(p)


def derive_state(roadmap, active_feature=None):
    """Derive .forge-state.json from roadmap.json + filesystem checks."""
    root = get_repo_root()
    state = {
        "active_feature": active_feature,
        "phase": None,
        "g1_metric_defined": False,
        "g2_plan_ready": False,
        "g3_dev_started": False,
        "g4_shipped": False,
        "g5_measuring": False,
        "g6_harvested": False,
    }

    if not active_feature or not roadmap:
        return state

    feature = find_feature(roadmap, active_feature)
    if not feature:
        return state

    status = feature.get("status", "")

    # Derive phase
    if status in ("draft", "planned"):
        state["phase"] = "planning"
    elif status == "in_progress":
        state["phase"] = "developing"
    elif status in ("shipped", "measuring"):
        state["phase"] = "harvesting"
    elif status == "concluded":
        state["phase"] = "done"

    # Check G1: metric.json exists and valid
    if root:
        metric_path = os.path.join(root, "project-roadmap", "features", active_feature, "metric.json")
        if os.path.exists(metric_path):
            try:
                with open(metric_path) as f:
                    metric = json.load(f)
                ok, _ = validate_metric(metric)
                state["g1_metric_defined"] = ok
            except Exception:
                pass

    # Check G2: trusted fastship Phase 1 completion
    fs_state = load_fastship_state(active_feature)
    orch_state = load_fastship_orchestrator_state(active_feature)
    state["g2_plan_ready"] = fastship_phase1_complete(active_feature, fs_state, orch_state)[0]

    # Check G3-G6 from roadmap status
    state["g3_dev_started"] = status in ("in_progress", "shipped", "measuring", "concluded")
    state["g4_shipped"] = status in ("shipped", "measuring", "concluded")
    state["g5_measuring"] = status in ("measuring", "concluded")

    # Check G6: harvest.json exists and valid
    if root:
        harvest_path = os.path.join(root, "project-roadmap", "features", active_feature, "harvest.json")
        if os.path.exists(harvest_path):
            try:
                with open(harvest_path) as f:
                    harvest = json.load(f)
                ok, _ = check_g6_harvest(active_feature, root)
                state["g6_harvested"] = ok
            except Exception:
                pass

    return state


def load_forge_state(slug=None):
    feature = _slug_id(slug) or current_feature_id()
    p = state_path(feature)
    if p and os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("active_feature", feature)
                return data
        except Exception:
            pass

    legacy = _load_json(legacy_forge_state_path())
    legacy_feature = _slug_id(legacy.get("active_feature")) if legacy else None
    if legacy and (not feature or legacy_feature == feature):
        feature = legacy_feature
        if feature:
            save_forge_state(legacy, feature)
        return legacy

    return {"active_feature": feature}


def save_forge_state(state, slug=None):
    feature = _slug_id(slug) or _slug_id((state or {}).get("active_feature")) or current_feature_id()
    if not feature:
        return
    state = dict(state or {})
    state["active_feature"] = feature
    p = state_path(feature)
    if not p:
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    registry = _load_forge_registry()
    features = registry.setdefault("features", {})
    rec = dict(features.get(feature) or {})
    rec.update({
        "slug": feature,
        "phase": state.get("phase"),
        "updated_at": datetime.now().isoformat(),
    })
    rec.setdefault("created_at", rec["updated_at"])
    features[feature] = rec
    registry["active_feature"] = feature
    _save_forge_registry(registry)


# ========== Roadmap.md Generation ==========

STATUS_EMOJI = {
    "draft": "📋",
    "planned": "📐",
    "in_progress": "🔄",
    "shipped": "🚀",
    "measuring": "⏳",
    "concluded": "✅",
}


def generate_roadmap_md(roadmap):
    """Generate roadmap.md content from roadmap.json."""
    proj = roadmap.get("project", {})
    lines = [
        f"# {proj.get('name', 'Project')} Roadmap",
        "",
        f"> North Star: {proj.get('north_star', '(undefined)')}",
        "",
    ]

    objectives = roadmap.get("objectives", [])
    features = roadmap.get("features", [])
    root = get_repo_root()  # Hoist outside loop

    def _tm_display(tm):
        return f"{tm.get('metric_id')}: {tm.get('baseline')}→{tm.get('target')}" if isinstance(tm, dict) else str(tm)

    for obj in objectives:
        lines.append(f"## {obj.get('name', '(unnamed)')}")
        lines.append(f"Target: {_tm_display(obj.get('target_metric', '(undefined)'))}")
        lines.append("")
        lines.append("| Feature | Status | Shipped | Harvest |")
        lines.append("|---------|--------|---------|---------|")

        obj_features = [f for f in features if f.get("objective_id") == obj.get("id")]
        for f in obj_features:
            status = f.get("status", "")
            emoji = STATUS_EMOJI.get(status, "")
            shipped = f.get("shipped_at", "-") or "-"
            if shipped != "-":
                shipped = shipped[5:]  # MM-DD
            harvest = "-"
            if f.get("harvest_due"):
                due = f["harvest_due"]
                harvest = f"Due {due[5:]}"
            if status == "concluded":
                if root:
                    hp = os.path.join(root, "project-roadmap", "features", f["slug"], "harvest.json")
                    if os.path.exists(hp):
                        try:
                            with open(hp) as hf:
                                hdata = json.load(hf)
                            harvest = hdata.get("verdict", "done")
                        except Exception:
                            harvest = "done"
            lines.append(f"| {f.get('name', f['slug'])} | {emoji} {status} | {shipped} | {harvest} |")

        lines.append("")

    # Objective 对齐
    align = []
    for obj in objectives:
        a = compute_objective_alignment(obj)
        if a:
            cur = "—" if a["current"] is None else a["current"]
            pct = "—" if a["pct"] is None else f"{a['pct']}%"
            align.append(f"| {obj.get('id')} | {a['metric_id']} | {a['baseline']}→{a['target']} | {cur} | {pct} | {a['status']} | {a['as_of'] or '—'} |")
    if align:
        lines += ["## Objective 对齐", "| Objective | Metric | Baseline→Target | Current | Progress | Status | As-of |",
                  "|---|---|---|---|---|---|---|", *align, ""]

    # Summary
    counts = {}
    for f in features:
        s = f.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1

    summary_parts = []
    for s in VALID_STATUSES:
        if counts.get(s, 0) > 0:
            summary_parts.append(f"{STATUS_EMOJI.get(s, '')} {s}: {counts[s]}")
    lines.append("## Summary")
    lines.append(" | ".join(summary_parts) if summary_parts else "No features yet.")
    lines.append("")

    return "\n".join(lines)


def save_roadmap_md(roadmap):
    """Write roadmap.md next to roadmap.json."""
    d = roadmap_dir()
    if not d:
        return
    content = generate_roadmap_md(roadmap)
    with open(os.path.join(d, "roadmap.md"), "w") as f:
        f.write(content)


# ========== CLI Commands ==========

def cmd_status():
    """Print global roadmap status + overdue harvest reminders."""
    roadmap = load_roadmap()
    if not roadmap:
        print("❌ No roadmap found. Run /forge init first.")
        sys.exit(0)

    proj = roadmap.get("project", {})
    print(f"🔥 {proj.get('name', 'Project')} — {proj.get('north_star', '')}")
    print()

    features = roadmap.get("features", [])
    for s in VALID_STATUSES:
        group = [f for f in features if f.get("status") == s]
        if group:
            print(f"  {STATUS_EMOJI.get(s, '')} {s}: {', '.join(f.get('name', f['slug']) for f in group)}")

    # Overdue reminders
    today = datetime.now().strftime("%Y-%m-%d")
    overdue = get_overdue_harvests(features, today)
    if overdue:
        print()
        print("⚠️  收益回收到期：")
        for f in overdue:
            shipped = f.get("shipped_at", "?")
            due = f.get("harvest_due", "?")
            print(f"  - {f.get('name', f['slug'])}（上线 {shipped}，回收日期 {due}）")

    # Measuring but not overdue
    measuring = [f for f in features
                 if f.get("status") == "measuring" and f not in overdue]
    if measuring:
        print()
        for f in measuring:
            due = f.get("harvest_due", "?")
            print(f"  ⏳ {f.get('name', f['slug'])} — 回收日期 {due}")

    # Active feature
    forge_state = load_forge_state()
    active = forge_state.get("active_feature")
    if active:
        print(f"\n  🎯 Active: {active}")

    n_orphans = removable_orphan_count()
    if n_orphans:
        print(f"\n🧹 {n_orphans} 个可清理的孤儿 worktree（干净+已合并）→ run /forge sweep-worktrees")


def bind_fastship_state_for_feature(slug):
    """Select the feature-scoped fastship session without mutating other sessions."""
    session_id = _slug_id(slug)
    home = fastship_state_home()
    if not home or not session_id:
        return

    session_dir = fastship_session_dir(session_id)
    os.makedirs(session_dir, exist_ok=True)
    p = fastship_state_path(session_id)
    st = _load_json(p)
    old_feature = st.get("forge_feature")
    st["forge_feature"] = session_id
    st.setdefault("session_id", session_id)
    st.setdefault("branch", get_current_branch())
    with open(p, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)

    registry_path = os.path.join(home, "registry.json")
    registry = _load_json(registry_path)
    sessions = registry.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    rec = dict(sessions.get(session_id) or {})
    rec.update({
        "id": session_id,
        "repo_root": get_repo_root(),
        "branch": get_current_branch(),
        "forge_feature": session_id,
        "updated_at": datetime.now().isoformat(),
    })
    rec.setdefault("requirement", session_id)
    rec.setdefault("created_at", rec["updated_at"])
    sessions[session_id] = rec
    registry["version"] = int(registry.get("version", 1) or 1)
    registry["current_session"] = session_id
    registry["sessions"] = sessions
    os.makedirs(os.path.dirname(registry_path), exist_ok=True)
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)

    if old_feature and old_feature != session_id:
        print(f"🔄 Fastship session rebound: {old_feature} → {session_id}")
    else:
        print(f"🔄 Fastship session selected: {session_id}")


def reset_fastship_state_for_feature(slug):
    """Backward-compatible name; now binds instead of resetting shared state."""
    bind_fastship_state_for_feature(slug)


def cmd_activate(slug):
    """Set active feature."""
    if not _compact_is_recent():
        if _activate_requires_compact():
            print("🧠 BLOCKED: 新 feature 前必须先 /compact，确保 context 干净。")
            print("   运行 /compact 后重试。")
            sys.exit(1)
        print("🧠 SUGGESTION: 建议新 feature 前先 /compact，确保 context 干净。")
        print("   未检测到最近 2 分钟内 /compact；继续 activate。")
    roadmap = load_roadmap()
    if not roadmap:
        print("❌ No roadmap found.")
        sys.exit(1)
    feature = find_feature(roadmap, slug)
    if not feature:
        print(f"❌ Feature '{slug}' not found in roadmap.")
        sys.exit(1)

    reset_fastship_state_for_feature(slug)

    state = derive_state(roadmap, slug)
    save_forge_state(state)
    print(f"✅ Active feature set to: {slug} (status: {feature['status']})")


def cmd_transition(slug, target_status):
    """Attempt state transition with gate check."""
    roadmap = load_roadmap()
    if not roadmap:
        print("❌ No roadmap found.")
        sys.exit(1)

    feature = find_feature(roadmap, slug)
    if not feature:
        print(f"❌ Feature '{slug}' not found.")
        sys.exit(1)

    current = feature["status"]
    if current == target_status:
        print(f"ℹ️  Feature '{slug}' is already in '{target_status}'.")
        sys.exit(0)

    root = get_repo_root()
    fs_state = load_fastship_state(slug)
    orch_state = load_fastship_orchestrator_state(slug)

    ok, reason = can_transition(slug, current, target_status, root, fs_state, orch_state)
    if not ok:
        print(f"🚫 Transition blocked: {reason}")
        sys.exit(1)

    # Apply transition
    feature["status"] = target_status
    now = datetime.now().strftime("%Y-%m-%d")

    if target_status == "shipped":
        feature["shipped_at"] = now
        # Read metric to calculate harvest_due
        metric_path = os.path.join(root, "project-roadmap", "features", slug, "metric.json") if root else None
        harvest_days = 7
        if metric_path and os.path.exists(metric_path):
            try:
                with open(metric_path) as f:
                    metric = json.load(f)
                harvest_days = metric.get("harvest_days", 7)
            except Exception:
                pass
        due = (datetime.now() + timedelta(days=harvest_days)).strftime("%Y-%m-%d")
        feature["harvest_due"] = due
        # Auto-transition to measuring (G5) — validate through can_transition
        ok_g5, reason_g5 = can_transition(slug, "shipped", "measuring", root, fs_state, orch_state)
        if not ok_g5:
            print(f"🚫 G5 auto-transition blocked: {reason_g5}")
            sys.exit(1)
        feature["status"] = "measuring"
        target_status = "measuring"

    if target_status == "concluded":
        feature["concluded_at"] = now

    save_roadmap(roadmap)
    save_roadmap_md(roadmap)

    # Re-derive forge state
    active = slug
    state = derive_state(roadmap, active)
    save_forge_state(state)

    # On delivery transitions, sweep ALL managed orphan worktrees (clean + truly
    # merged). Best-effort — wrapped so a cleanup error never blocks the transition.
    # Full sweep (not per-feature) because git forbids removing the worktree you're
    # standing in: reaping every *other* delivered feature's orphan here converges
    # to zero across deliveries. The just-shipped feature's own worktree (if you're
    # inside it) is caught later by `/forge status` + manual `/forge sweep-worktrees`.
    if target_status in ("shipped", "measuring", "concluded"):
        try:
            res = sweep_worktrees(root)
            if res.get("removed"):
                _print_sweep(res)
        except Exception as e:
            print(f"⚠️  worktree 自动清理跳过（非致命）：{e}")

    print(f"✅ {slug}: {current} → {target_status}")


def cmd_generate_view():
    """Regenerate roadmap.md."""
    roadmap = load_roadmap()
    if not roadmap:
        print("❌ No roadmap found.")
        sys.exit(1)
    save_roadmap_md(roadmap)
    print("✅ roadmap.md regenerated.")


def cmd_reset():
    """Clear active feature state."""
    registry = _load_forge_registry()
    registry["active_feature"] = None
    _save_forge_registry(registry)
    print("✅ Active feature cleared.")


def cmd_doctor():
    """Validate forge installation and roadmap gate inputs without mutation."""
    root = get_repo_root()
    if not root:
        print("🚫 Forge doctor: cannot determine repo root")
        sys.exit(1)

    roadmap = load_roadmap()
    if not roadmap:
        print("🚫 Forge doctor: project-roadmap/roadmap.json not found")
        sys.exit(1)

    errors = []
    warnings = []
    features = roadmap.get("features", [])

    for feature in features:
        slug = feature.get("slug")
        status = feature.get("status")
        if not slug:
            errors.append("roadmap feature missing slug")
            continue
        if status not in VALID_STATUSES:
            errors.append(f"{slug}: invalid status '{status}'")
        ok, reason = check_g1_metric(slug, root)
        if not ok:
            errors.append(reason)

    features_dir = os.path.join(root, "project-roadmap", "features")
    roadmap_slugs = {f.get("slug") for f in features}
    if os.path.isdir(features_dir):
        for entry in sorted(os.listdir(features_dir)):
            metric_path = os.path.join(features_dir, entry, "metric.json")
            if os.path.isdir(os.path.join(features_dir, entry)) and os.path.exists(metric_path):
                if entry not in roadmap_slugs:
                    warnings.append(f"{entry}: metric.json exists but feature is not listed in roadmap.json")

    if warnings:
        print("⚠️  Forge doctor warnings:")
        for warning in warnings:
            print(f"  - {warning}")

    if errors:
        print("🚫 Forge doctor failed:")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)

    print(f"✅ Forge doctor passed ({len(features)} roadmap feature(s) checked)")


def cmd_audit_month(month, strict=False):
    """Audit monthly plan files against Forge metric artifacts."""
    if not re.match(r"^\d{4}-\d{2}$", month):
        print("Usage: forge_gate.py audit-month YYYY-MM [--strict]")
        sys.exit(1)

    root = get_repo_root()
    if not root:
        print("🚫 Forge audit: cannot determine repo root")
        sys.exit(1)

    roadmap = load_roadmap()
    if not roadmap:
        print("🚫 Forge audit: project-roadmap/roadmap.json not found")
        sys.exit(1)

    plans = month_plan_slugs(root, month)
    metrics = metric_slugs(root)
    roadmap_slugs = sorted(f.get("slug") for f in roadmap.get("features", []) if f.get("slug"))

    missing_metric_for_plan = [slug for slug in plans if slug not in metrics]
    metric_not_in_roadmap = [slug for slug in metrics if slug not in roadmap_slugs]
    roadmap_without_metric = [slug for slug in roadmap_slugs if slug not in metrics]

    print(f"📊 Forge audit {month}")
    print(f"  plans:   {len(plans)}")
    print(f"  metrics: {len(metrics)}")
    print(f"  roadmap: {len(roadmap_slugs)}")

    if missing_metric_for_plan:
        label = "🚫 Missing metric.json for plan" if strict else "⚠️  Plans without metric.json"
        print(f"\n{label}:")
        for slug in missing_metric_for_plan:
            print(f"  - {slug}")

    if metric_not_in_roadmap:
        print("\n⚠️  metric.json exists but feature is not listed in roadmap.json:")
        for slug in metric_not_in_roadmap:
            print(f"  - {slug}")

    if roadmap_without_metric:
        print("\n🚫 roadmap feature missing metric.json:")
        for slug in roadmap_without_metric:
            print(f"  - {slug}")

    if roadmap_without_metric or (strict and missing_metric_for_plan):
        print("\n🚫 Forge audit failed")
        sys.exit(1)

    print("\n✅ Forge audit completed")


def month_plan_slugs(root, month):
    """Return slugs from docs/superpowers/plans/YYYY-MM-DD-*.md."""
    plans_dir = os.path.join(root, "docs", "superpowers", "plans")
    if not os.path.isdir(plans_dir):
        return []
    pattern = re.compile(rf"^{re.escape(month)}-\d{{2}}-(.+)\.md$")
    slugs = []
    for name in sorted(os.listdir(plans_dir)):
        match = pattern.match(name)
        if match:
            slugs.append(match.group(1))
    return slugs


def metric_slugs(root):
    features_dir = os.path.join(root, "project-roadmap", "features")
    if not os.path.isdir(features_dir):
        return []
    slugs = []
    for entry in sorted(os.listdir(features_dir)):
        metric_path = os.path.join(features_dir, entry, "metric.json")
        if os.path.isdir(os.path.join(features_dir, entry)) and os.path.exists(metric_path):
            slugs.append(entry)
    return slugs


# ========== Hook Handlers ==========

def hook_pre_edit():
    """Protect derived forge state and roadmap.json status from manual tampering."""
    data = read_stdin()
    file_path = data.get("tool_input", {}).get("file_path", "")
    normalized = file_path.replace("\\", "/")
    if ".forge-state.json" in normalized or "/.claude/forge-state/" in normalized or normalized.startswith(".claude/forge-state/"):
        print("🚫 Forge Gate: derived forge state is managed by forge_gate.py. Do not edit manually.")
        sys.exit(1)

    if file_path.endswith("roadmap.json") and "project-roadmap" in file_path:
        tool_input = data.get("tool_input", {})
        old_str = tool_input.get("old_string", "")
        new_str = tool_input.get("new_string", "")
        content = tool_input.get("content", "")
        combined = old_str + new_str + content
        if re.search(r'"status"\s*:', combined):
            print("🚫 Forge Gate: roadmap.json の status フィールドを直接編集することはできません。")
            print("   状態遷移は forge_gate.py transition <slug> <status> を使用してください。")
            print("   例: python3 forge_gate.py transition my-feature planned")
            sys.exit(1)


def hook_post_edit():
    """Detect metric.json / harvest.json / roadmap.json writes."""
    data = read_stdin()
    file_path = data.get("tool_input", {}).get("file_path", "")

    if not file_path:
        return

    # Detect roadmap.json change → regenerate view
    if file_path.endswith("roadmap.json") and "project-roadmap" in file_path:
        roadmap = load_roadmap()
        if roadmap:
            save_roadmap_md(roadmap)

    # Detect metric.json write → validate
    if file_path.endswith("metric.json") and "project-roadmap" in file_path:
        try:
            with open(file_path) as f:
                metric = json.load(f)
            ok, errors = validate_metric(metric)
            if not ok:
                print(f"⚠️  metric.json validation issues: {'; '.join(errors)}")
        except Exception as e:
            print(f"⚠️  Could not validate metric.json: {e}")

    # Detect harvest.json write → validate
    if file_path.endswith("harvest.json") and "project-roadmap" in file_path:
        try:
            with open(file_path) as f:
                harvest = json.load(f)
            ok, errors = validate_harvest(harvest)
            if not ok:
                print(f"⚠️  harvest.json validation issues: {'; '.join(errors)}")
        except Exception as e:
            print(f"⚠️  Could not validate harvest.json: {e}")


def hook_post_bash():
    """Check overdue harvests after bash commands (lightweight reminder)."""
    roadmap = load_roadmap()
    if not roadmap:
        return
    features = roadmap.get("features", [])
    today = datetime.now().strftime("%Y-%m-%d")
    overdue = get_overdue_harvests(features, today)
    if overdue:
        names = ", ".join(f.get("name", f["slug"]) for f in overdue)
        print(f"⚠️  Forge: 收益回收到期 → {names}. Run /forge harvest <feature>.")


# ========== Main Dispatch ==========

def main():
    if len(sys.argv) < 2:
        print("Usage: forge_gate.py <action> [args...]")
        sys.exit(1)

    action = sys.argv[1]

    if action == "check-g1":
        if len(sys.argv) < 3:
            print("Usage: forge_gate.py check-g1 <slug>")
            sys.exit(1)
        root = get_repo_root()
        ok, reason = check_g1_metric(sys.argv[2], root)
        if ok:
            print(f"✅ Gate 1 passed for {sys.argv[2]}")
        else:
            print(f"🚫 {reason}")
            sys.exit(1)
    elif action == "status":
        cmd_status()
    elif action == "doctor":
        cmd_doctor()
    elif action == "audit-month":
        if len(sys.argv) < 3:
            print("Usage: forge_gate.py audit-month YYYY-MM [--strict]")
            sys.exit(1)
        cmd_audit_month(sys.argv[2], strict="--strict" in sys.argv[3:])
    elif action == "activate":
        if len(sys.argv) < 3:
            print("Usage: forge_gate.py activate <slug>")
            sys.exit(1)
        cmd_activate(sys.argv[2])
    elif action == "transition":
        if len(sys.argv) < 4:
            print("Usage: forge_gate.py transition <slug> <status>")
            sys.exit(1)
        cmd_transition(sys.argv[2], sys.argv[3])
    elif action == "generate-view":
        cmd_generate_view()
    elif action == "track":
        a = sys.argv[2:]
        if a[:1] == ["--objective"]:
            kind, oid, rest = "objectives", (a[1] if len(a) > 1 else ""), a[2:]
        else:
            kind, oid, rest = "features", (a[0] if a else ""), a[1:]
        if not oid or len(rest) < 2:
            print("Usage: track <slug> <metric_id> <as_of> | track --objective <id> <metric_id> <as_of>")
            sys.exit(1)
        sys.exit(cmd_track(kind, oid, rest[0], rest[1]))
    elif action == "analyze":
        a = sys.argv[2:]
        if a[:1] == ["--objective"]:
            oid = a[1] if len(a) > 1 else ""
            kind = "objectives"
        else:
            oid = a[0] if a else ""
            kind = "features"
        if not oid:
            print("Usage: analyze <slug> | analyze --objective <id>")
            sys.exit(1)
        sys.exit(cmd_analyze(kind, oid))
    elif action == "sweep-worktrees":
        cmd_sweep_worktrees(dry_run="--dry-run" in sys.argv)
    elif action == "reset":
        cmd_reset()
    elif action == "pre_edit":
        hook_pre_edit()
    elif action == "post_edit":
        hook_post_edit()
    elif action == "post_bash":
        hook_post_bash()
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)


if __name__ == "__main__":
    main()
