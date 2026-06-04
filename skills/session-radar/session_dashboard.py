#!/usr/bin/env python3
"""Session Radar — stdlib-only dashboard for ALL local Claude sessions.

Scans ~/.claude/projects/*/*.jsonl (foreground sessions) + ~/.claude/jobs/
(background daemon tasks) and surfaces, per session: liveness
(active/idle/dormant/errored, with bg jobs trusting the job's own state),
the repo/worktree/branch it is CURRENTLY acting on (derived from the transcript
tail, not the opening title), and the opening-intent -> current-action drift.

Reuses the stdlib web-shell shape from skills/forge/forge_dashboard.py
(ThreadingHTTPServer + /api/state JSON + client-rendered HTML). No third-party deps.
"""
import argparse
import glob
from collections import Counter
import json
import os
import re
import shutil
import signal
import tempfile
import threading
import concurrent.futures
import socket
import subprocess
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 7575
WINDOW_MIN_DEFAULT = 120
ACTIVE_S = 90
IDLE_S = 600

_SHELL_TAGS = ("<command-message>", "<command-name>", "<command-args>", "<local-command-caveat>")
_TAG_BLOCK_RE = re.compile(
    r"<(command-message|command-name|command-args|local-command-caveat)>(.*?)</\1>", re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def _ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def strip_command_shell(text):
    """Hard problem #1: turn a raw user message into the real human intent.

    If the message wraps a slash command, prefer the literal <command-args>
    (what the human actually typed). Otherwise drop every command-shell block
    and any residual tags. Plain prompts (no shell tags) pass through verbatim
    so we never mangle legitimate angle brackets in human text.
    """
    if not isinstance(text, str):
        return ""
    if not any(t in text for t in _SHELL_TAGS):
        return _ws(text)
    args = [a.strip() for a in _ARGS_RE.findall(text) if a.strip()]
    if args:
        return _ws(" ".join(args))
    cleaned = _TAG_BLOCK_RE.sub("", text)
    cleaned = _ANY_TAG_RE.sub("", cleaned)
    return _ws(cleaned)


def _content_text(content):
    """Flatten a message.content (str OR list of typed blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text") or "" for b in content
            if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_tool_result(content):
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def extract_opening(objs, limit=60):
    """Hard problem #1: first genuine human prompt, command-shell stripped."""
    for o in objs[:limit]:
        if o.get("type") != "user":
            continue
        msg = o.get("message") or {}
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if _is_tool_result(content):
            continue
        cleaned = strip_command_shell(_content_text(content))
        if cleaned:
            return cleaned
    return ""


_ERROR_RE = re.compile(
    r"^(api error|request failed|error:|overloaded|rate limit|http\s*[45]\d\d|[45]\d\d\s)",
    re.IGNORECASE)


def is_error_text(text):
    """Hard problem #3: detect an error tail that must NOT be reported as work."""
    return bool(text) and bool(_ERROR_RE.match(text.strip()))


def _assistant_summary(content):
    """Return (text, tools) from an assistant message.content."""
    if isinstance(content, str):
        return content.strip(), []
    text_parts, tools = [], []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                name = b.get("name") or "tool"
                inp = b.get("input") or {}
                hint = ""
                for k in ("file_path", "path", "command", "pattern", "description", "query"):
                    v = inp.get(k)
                    if isinstance(v, str) and v.strip():
                        hint = v.strip()[:40]
                        break
                tools.append(f"{name}({hint})" if hint else name)
            elif b.get("type") == "text" and b.get("text"):
                text_parts.append(b["text"])
    return " ".join(text_parts).strip(), tools


def extract_activity(objs):
    """Walk the transcript tail to derive what the session is actually DOING.

    "NOW" should be a real ACTION, not chatter. So:
      - latest assistant turn has a tool_use  -> show that tool action (in flight)
      - latest assistant turn is an error text -> '⚠ …' + errored=True
      - latest assistant turn is plain prose   -> the session replied / went idle;
        show its most recent real tool action instead (what it last DID). Only when
        the session has used no tool at all do we fall back to the reply text,
        prefixed '💬' to mark it as a reply rather than an action.
    Also returns the latest cwd/branch from the tail.
    """
    cwd = branch = None
    latest_kind = None          # 'tools' | 'error' | 'text'
    latest_tools = latest_reply = None
    prior_tool = None           # most recent tool action at/behind the tail
    errored = False
    scanned = 0
    for o in reversed(objs):
        if cwd is None and o.get("cwd"):
            cwd, branch = o.get("cwd"), o.get("gitBranch")
        if o.get("type") == "assistant":
            text, tools = _assistant_summary((o.get("message") or {}).get("content"))
            if latest_kind is None:
                if tools:
                    latest_kind, latest_tools = "tools", " · ".join(tools[:3])
                elif text and is_error_text(text):
                    latest_kind, errored = "error", True
                    latest_reply = "⚠ " + _ws(text)[:80]
                elif text:
                    latest_kind = "text"
                    latest_reply = "💬 " + _ws(text)[:80]
            if prior_tool is None and tools:
                prior_tool = " · ".join(tools[:3])
            scanned += 1
            if scanned >= 60:
                break
        if cwd is not None and prior_tool is not None and latest_kind is not None:
            break
    if latest_kind == "tools":
        activity = latest_tools
    elif latest_kind == "error":
        activity = latest_reply
    else:  # plain-text reply, or no assistant turn at all
        activity = prior_tool or latest_reply or ""
    return activity, cwd, branch, errored


LIVENESS_LABELS = {
    "active": "🟢 active",
    "idle": "🟡 idle",
    "dormant": "⚪ dormant",
    "errored": "🔴 errored",
    "working": "🟢 working",   # bg alive, just quiet between turns
    "blocked": "🟠 blocked",   # bg awaiting input/permission
    "done": "✅ done",
    "unknown": "❔ unknown",   # bg with no/unrecognized state.json state
}
_BG_ALIVE = ("active", "running", "in_progress")
_BG_WAIT = ("blocked", "waiting", "paused")
_BG_DONE = ("done", "completed", "finished", "stopped")


def bg_jobs(home):
    """Map 8-char job id -> {state, intent, cwd, updated_at, link_path}.

    A job dir with no readable state.json is still surfaced (state=None) so the
    session is correctly flagged as background. Authoritative source for hard
    problem #2: the job's own `state`, not the transcript mtime.
    """
    out = {}
    jdir = os.path.join(os.path.expanduser(home), "jobs")
    try:
        entries = os.listdir(jdir)
    except OSError:
        return out
    for d in entries:
        p = os.path.join(jdir, d)
        if not os.path.isdir(p):
            continue
        info = {"state": None, "intent": None, "cwd": None, "updated_at": None, "link_path": None}
        sp = os.path.join(p, "state.json")
        if os.path.exists(sp):
            try:
                with open(sp, encoding="utf-8") as f:
                    s = json.load(f)
                info["state"] = s.get("state")
                info["intent"] = s.get("intent")
                info["cwd"] = s.get("cwd") or s.get("originCwd")
                info["updated_at"] = s.get("updatedAt")
                info["link_path"] = s.get("linkScanPath")
            except Exception:
                pass
        out[d] = info
    return out


def liveness(age_s, is_bg=False, bg_state=None, errored=False):
    """Liveness bucket. Errors win. Background jobs are classified by their OWN
    state and NEVER by transcript mtime (hard problem #2: a bg job is silent
    between turns, and a stateless job's age is meaningless). A bg job with no
    recognizable state is 'unknown', not 'active'. Foreground sessions, which
    have no authoritative state, fall back to mtime buckets."""
    if errored:
        return "errored"
    if is_bg:
        s = str(bg_state).lower() if bg_state else ""
        if s in _BG_ALIVE:
            return "working"
        if s in _BG_WAIT:
            return "blocked"
        if s in _BG_DONE:
            return "done"
        return "unknown"   # bg with missing/unrecognized state — never trust mtime
    if age_s < ACTIVE_S:
        return "active"
    if age_s < IDLE_S:
        return "idle"
    return "dormant"


def worktree_of(cwd):
    if not cwd:
        return None
    m = re.search(r"/\.claude/worktrees/([^/]+)", cwd) or re.search(r"/worktrees/([^/]+)", cwd)
    return m.group(1) if m else None


def repo_of(cwd):
    if not cwd:
        return "—"
    wt = worktree_of(cwd)
    m = re.search(r"/([^/]+)/\.claude/worktrees/", cwd)
    base = m.group(1) if m else (os.path.basename(cwd.rstrip("/")) or cwd)
    return f"{base} ⟨wt:{wt}⟩" if wt else base


def _git(args, cwd):
    """Run a git subprocess in cwd; return stripped stdout or None."""
    try:
        r = subprocess.run(["git", "-C", cwd] + args,
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


_PROJECT_CACHE = {}
_PATH_RE = re.compile(r"/(?:[^/]+/)*[^/]+/\.claude/worktrees/")


def _project_from_path(cwd):
    """Path-only fallback: the repo dir before /.claude/worktrees, else basename."""
    m = re.search(r"/([^/]+)/\.claude/worktrees/", cwd)
    return m.group(1) if m else (os.path.basename(cwd.rstrip("/")) or cwd)


def project_of(cwd):
    """The MAIN git repo a cwd belongs to — worktrees collapse to their parent
    repo via `git rev-parse --git-common-dir` (so a worktree placed under another
    repo's tree still attributes to its true owner). Falls back to path parsing
    when git is unavailable (e.g. fake test cwds). Cached per cwd."""
    if not cwd:
        return "(no cwd)"
    if cwd in _PROJECT_CACHE:
        return _PROJECT_CACHE[cwd]
    proj = None
    gcd = _git(["rev-parse", "--git-common-dir"], cwd)
    if gcd:
        if not os.path.isabs(gcd):
            gcd = os.path.normpath(os.path.join(cwd, gcd))
        proj = os.path.basename(os.path.dirname(gcd)) or None
    if not proj:
        proj = _project_from_path(cwd)
    _PROJECT_CACHE[cwd] = proj
    return proj


_WTCD_RE = re.compile(r"(?:\bWT=|\bcd\s+)(/(?:Users|home)/[^\s;&|'\"]+)")


def _now_other_repo(objs, project):
    """If the action NOW shows (the most recent tool_use) explicitly targets a
    different repo via `WT=<abs>` or `cd <abs>` in its command, return that repo's
    name. Concrete evidence taken from the command text — not a guess. Surfaces
    the 'sits in repo A, operating on repo B' case (e.g. a claude-skills task run
    from an aifriends worktree, or a session that cd'd into another checkout)."""
    if not project:
        return None
    for o in reversed(objs):
        if o.get("type") != "assistant":
            continue
        content = (o.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        cmds = [str((b.get("input") or {}).get("command", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use"]
        if not cmds:
            continue  # text-only turn — keep looking for the most recent ACTION
        for cmd in cmds:
            for p in _WTCD_RE.findall(cmd):
                other = project_of(p.rstrip("/"))
                if other and other != project and other != "(no cwd)":
                    return other
        return None  # this is the latest tool action; it had no cross-repo cd
    return None


# Latin words (>=3 chars) AND individual CJK ideographs (which are not
# whitespace-separated). Without the CJK class a Chinese opening like
# "做 session 维度可视化雷达" would yield zero tokens and drift would never fire.
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}|[一-鿿㐀-䶿]")
_STOP = {"the", "and", "for", "make", "session", "this", "that", "with"}


def _tokens(s):
    return {w for w in _TOKEN_RE.findall((s or "").lower()) if w not in _STOP}


def compute_drift(opening, now):
    """Heuristic drift HINT (no LLM): low token overlap between the opening intent
    and the current action. CJK-aware so Chinese openings participate. This is a
    best-effort highlight — the real signal is the opening/now columns shown
    side-by-side; the boolean just nudges the eye. Both sides must be non-empty."""
    o, n = _tokens(opening), _tokens(now)
    if not o or not n:
        return False
    return (len(o & n) / len(o | n)) < 0.2


# ── Work-unit summary. Distill the CONCRETE thing a session is doing
# (feature/bugfix/refactor) from self-authored signals across the transcript,
# NOT the latest message. This heuristic layer builds the structured skeleton
# (type badge, title, in-progress step, detail, progress); the LLM layer below
# refines the one-line headline. ──
_SIG_WINDOW = 400  # recent events that define the CURRENT unit of work
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")
_EXT_RE = re.compile(r"\.(md|py|txt|json|ts|tsx|js|java|kt)$")
_PREFIX_RE = re.compile(r"^\s*([a-z]+)\s*[/:!(]")
_HEREDOC_RE = re.compile(r"<<['\"]?[A-Z]+['\"]?\s*\n(.*?)\n", re.DOTALL)
_DASH_M_RE = re.compile(r"-m\s+(['\"])(.*?)\1", re.DOTALL)
_PLAN_PATH_RE = re.compile(r"/plans?/([^/]+\.md)")

_WORK_TYPES = {
    "feat": ("feature", "🟢"), "feature": ("feature", "🟢"),
    "fix": ("bugfix", "🔴"), "bug": ("bugfix", "🔴"),
    "bugfix": ("bugfix", "🔴"), "hotfix": ("bugfix", "🔴"),
    "refactor": ("refactor", "🟣"),
    "perf": ("perf", "⚡"), "opt": ("perf", "⚡"), "optimize": ("perf", "⚡"),
    "chore": ("chore", "⚪"),
    "docs": ("docs", "📘"), "doc": ("docs", "📘"),
    "test": ("test", "🧪"),
}
_DEFAULT_TYPE = ("task", "▫️")
_GENERIC_BRANCH = {"main", "master", "head", "develop", "dev", "trunk", ""}


def _humanize(slug):
    s = _DATE_PREFIX_RE.sub("", slug or "")
    s = _EXT_RE.sub("", s)
    s = s.replace("-", " ").replace("_", " ").replace("/", " ")
    return _ws(s)


def _commit_subject(cmd):
    """First-line subject of a `git commit -m …` command (inline or heredoc), else None."""
    if not isinstance(cmd, str) or "git commit" not in cmd:
        return None
    m = _HEREDOC_RE.search(cmd)
    if m and m.group(1).strip():
        return _ws(m.group(1).strip().splitlines()[0])[:60]
    m = _DASH_M_RE.search(cmd)
    if m and m.group(2).strip():
        return _ws(m.group(2).strip().splitlines()[0])[:60]
    return None


def _type_from(s):
    m = _PREFIX_RE.match((s or "").lower())
    if m and m.group(1) in _WORK_TYPES:
        return _WORK_TYPES[m.group(1)]
    return None


def work_type(branch, commits, opening):
    """Work-unit TYPE: branch prefix → conventional-commit prefix → opening keyword → task."""
    t = _type_from(branch)
    if t:
        return t
    for c in commits:
        t = _type_from(c)
        if t:
            return t
    low = (opening or "").lower()
    if any(k in low for k in ("修复", "fix", "bug", "报错", "错误", "hotfix")):
        return _WORK_TYPES["fix"]
    if any(k in low for k in ("重构", "refactor")):
        return _WORK_TYPES["refactor"]
    if any(k in low for k in ("文档", "docs", "readme")):
        return _WORK_TYPES["docs"]
    return _DEFAULT_TYPE


def work_title(branch, plan_refs, opening):
    """Stable unit-of-work NAME: feature-branch slug → plan filename → opening."""
    b = (branch or "").strip()
    slug = b.split("/", 1)[1] if "/" in b else b
    if b.lower() not in _GENERIC_BRANCH and slug:
        return _humanize(slug)
    if plan_refs:
        return _humanize(plan_refs[0])
    if opening:
        return opening[:60]
    return "—"


def _reconstruct_tasks(creates, updates):
    """Event-source the task list: TaskCreate defines tasks in creation order
    (taskId = '1','2',… by order); TaskUpdate{taskId,status} transitions them.
    Returns a normalized [{content, activeForm, status}] list."""
    tasks = []
    for i, c in enumerate(creates):
        subj = (c.get("subject") or "").strip()
        tasks.append({"id": str(i + 1), "content": subj,
                      "activeForm": (c.get("activeForm") or subj).strip(),
                      "status": "pending"})
    by_id = {t["id"]: t for t in tasks}
    for u in updates:
        t = by_id.get(str(u.get("taskId")))
        if t and u.get("status"):
            t["status"] = u["status"]
    return tasks


def work_doing(todos):
    """The live concrete sub-step: the in-progress item's activeForm (the agent's
    own 'what I'm doing now'); else the first pending; else ''."""
    for status in ("in_progress", "pending"):
        for t in todos or []:
            if isinstance(t, dict) and t.get("status") == status:
                return _ws(t.get("activeForm") or t.get("content") or "")[:60]
    return ""


def work_progress(todos):
    total = sum(1 for t in (todos or []) if isinstance(t, dict))
    if not total:
        return ""
    done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
    return f"{done}/{total}"


def work_detail(edit_counts, commits):
    """Aggregate evidence: most-edited file (+count, +other-file count) · N commits."""
    bits = []
    if edit_counts:
        top, n = edit_counts.most_common(1)[0]
        seg = f"{top}×{n}" if n > 1 else top
        extra = len(edit_counts) - 1
        if extra > 0:
            seg += f"(+{extra})"
        bits.append(seg)
    if commits:
        bits.append(f"{len(commits)} commit" + ("s" if len(commits) > 1 else ""))
    return " · ".join(bits)


def _collect_signals(objs):
    """One pass over the transcript. Task lifecycle (TaskCreate/TaskUpdate or
    TodoWrite) is read across ALL events (taskIds are creation-ordered); commits,
    edited-file tally and plan refs are read from the recent _SIG_WINDOW slice."""
    todowrite_todos = None
    creates, updates = [], []
    commits, plan_refs = [], []
    edit_counts = Counter()
    last_todo_idx = last_task_idx = -1
    n = len(objs)
    cutoff = n - _SIG_WINDOW
    for idx, o in enumerate(objs):
        if o.get("type") != "assistant":
            continue
        content = (o.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        recent = idx >= cutoff
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name = b.get("name") or ""
            inp = b.get("input") or {}
            if name == "TodoWrite" and isinstance(inp.get("todos"), list):
                todowrite_todos = inp["todos"]
                last_todo_idx = idx
            elif name == "TaskCreate":
                creates.append(inp)
                last_task_idx = idx
            elif name == "TaskUpdate":
                updates.append(inp)
                last_task_idx = idx
            if not recent:
                continue
            if name == "Bash":
                subj = _commit_subject(inp.get("command", ""))
                if subj:
                    commits.append(subj)
            elif name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
                fp = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
                if isinstance(fp, str) and fp.strip():
                    edit_counts[os.path.basename(fp.rstrip("/"))] += 1
            if name in ("Read", "Edit", "Write"):
                fp = inp.get("file_path") or inp.get("path") or ""
                m = _PLAN_PATH_RE.search(str(fp))
                if m:
                    plan_refs.append(m.group(1))
    # Prefer the MOST RECENT task source by event position, so a stale early
    # TodoWrite snapshot can't mask a later TaskCreate/TaskUpdate lifecycle (and
    # vice-versa). Ties / TodoWrite-only -> TodoWrite; Task-only -> reconstructed.
    if todowrite_todos is not None and last_todo_idx >= last_task_idx:
        todos = todowrite_todos
    else:
        todos = _reconstruct_tasks(creates, updates)
    commits.reverse()  # newest-first
    return {"todos": todos, "commits": commits, "edit_counts": edit_counts, "plan_refs": plan_refs}


def summarize_session(objs, job, opening, branch, errored, fallback_activity):
    """Heuristic work-unit skeleton: {type, icon, title, doing, detail, progress,
    summary, source}. Background jobs summarize from their human intent. This is
    deterministic; the LLM layer may later refine `summary` (and set source=llm)."""
    if job is not None:
        intent = _ws(job.get("intent") or opening or "")
        ty, icon = work_type(branch, [], intent)
        title = intent[:60] if intent else (fallback_activity or "—")
        summary = f"{icon} {ty} · {title}" if title != "—" else f"{icon} {ty}"
        return {"type": ty, "icon": icon, "title": title, "doing": "",
                "detail": f"[bg:{job.get('state') or '?'}]", "progress": "",
                "summary": _ws(summary), "source": "heuristic"}
    sig = _collect_signals(objs)
    ty, icon = work_type(branch, sig["commits"], opening)
    title = work_title(branch, sig["plan_refs"], opening)
    doing = work_doing(sig["todos"])
    detail = work_detail(sig["edit_counts"], sig["commits"])
    progress = work_progress(sig["todos"])
    if errored and not doing:
        doing = "⚠ 出错中断"
    if title == "—" and not doing and not detail:
        title = fallback_activity or "—"
    summary = f"{icon} {ty} · {title}"
    if doing:
        summary += f" — 正在{doing}"
    return {"type": ty, "icon": icon, "title": title, "doing": doing,
            "detail": detail, "progress": progress, "summary": _ws(summary),
            "source": "heuristic"}


# ── LLM headline layer. The user chose generative summaries over pure heuristics;
# we shell out to the local `claude` CLI in headless print mode (Haiku) to turn the
# structured signals into one human-readable line. Injectable (tests pass a stub),
# cached by signal fingerprint, and a no-op fallback to the heuristic when `claude`
# is absent / disabled / errors. Background jobs are NOT sent to the LLM (their
# human-authored intent is already a summary). ──
DEFAULT_LLM_MODEL = "claude-haiku-4-5"
_LLM_TIMEOUT_S = 45        # one cold `claude` call is ~15s; allow margin under concurrency
_LLM_CACHE = {}            # fingerprint -> summary string
_LLM_PENDING = set()       # fingerprints currently computing in the background
_LLM_LOCK = threading.Lock()   # guards _LLM_CACHE + _LLM_PENDING across request/worker threads
_LLM_POOL = None
# Signature of THIS tool's own LLM prompt. Each `claude -p` summary call writes a
# fresh transcript into ~/.claude/projects, which the radar would then try to
# summarize — an amplifying feedback loop. Exclude our own helper sessions.
_RADAR_PROMPT_SIG = "你是编程会话雷达"


def _llm_pool():
    global _LLM_POOL
    if _LLM_POOL is None:
        # 2 workers. Each `claude -p` cold-start is ~20s; two in parallel measured
        # fine (~18s each). Keep it modest so cold-starts don't pile up; the
        # fingerprint cache makes repeat pulls free, so the visible set fills in
        # ~1 min and stays warm.
        _LLM_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2,
                                                          thread_name_prefix="radar-llm")
    return _LLM_POOL


def _is_radar_helper_session(objs):
    """True if this transcript is one of the radar's own `claude -p` summary calls
    (its opening is our prompt) — exclude to avoid a feedback loop. These headless
    transcripts begin with queue-operation/attachment objects before the user
    message, so reuse extract_opening (which already locates the first real prompt)
    rather than peeking a fixed prefix."""
    return _RADAR_PROMPT_SIG in (extract_opening(objs, limit=80) or "")


def _cache_get(fp):
    with _LLM_LOCK:
        return _LLM_CACHE.get(fp)


def _cache_set(fp, phrase):
    with _LLM_LOCK:
        _LLM_CACHE[fp] = phrase


def _claim_pending(fp):
    """Atomic: return True (and mark pending) only if `fp` is neither cached nor
    already pending — so under ThreadingHTTPServer exactly one worker is scheduled
    per fingerprint. False means someone else owns it / it's already done."""
    with _LLM_LOCK:
        if fp in _LLM_CACHE or fp in _LLM_PENDING:
            return False
        _LLM_PENDING.add(fp)
        return True


def _release_pending(fp):
    with _LLM_LOCK:
        _LLM_PENDING.discard(fp)


def claude_available():
    return shutil.which("claude") is not None


def make_claude_llm(model=DEFAULT_LLM_MODEL):
    """Return llm(prompt)->str|None that calls `claude -p --model <model>` (prompt
    via stdin). Returns the first non-empty output line, or None on any failure.

    Runs the child in its OWN process group and, on timeout, SIGKILLs the whole
    group. The `claude` CLI re-execs into a versioned node child; subprocess
    timeout alone would kill the wrapper but leak that child, so slow calls would
    pile up as zombies and contend for CPU until every later call also times out
    (a self-inflicted death spiral). Killing the group prevents that."""
    def _llm(prompt):
        try:
            # Run in a NEUTRAL cwd (tempdir), not the scanned project: in a repo,
            # `claude` loads the whole project context (CLAUDE.md, files) on every
            # call — ~30% slower and it sometimes answers ABOUT the project instead
            # of distilling. A neutral cwd is faster (~19s vs ~28s) and on-task.
            p = subprocess.Popen(["claude", "-p", "--model", model],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True,
                                 cwd=tempfile.gettempdir(), start_new_session=True)
        except Exception:
            return None
        try:
            out, _ = p.communicate(prompt, timeout=_LLM_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
            try:
                p.communicate(timeout=5)
            except Exception:
                pass
            return None
        except Exception:
            return None
        if p.returncode != 0:
            return None
        for line in (out or "").splitlines():
            line = _ws(line)
            if line:
                return line[:80]
        return None
    return _llm


def _fingerprint(sid, work):
    # Key on type+title+doing (the WHAT), NOT detail. detail churns every tool
    # call (edit tally / commit count) on an active session, which would
    # invalidate the cached headline faster than the ~15s LLM call can refresh
    # it — the row would never escape the heuristic. title already encodes the
    # branch slug, doing the in-progress step; a branch/type/step change still
    # re-summarizes. Model identity is fixed per process, so it is not keyed.
    return "|".join([sid, work.get("type", ""), work.get("title", ""),
                     work.get("doing", "")])


def build_llm_prompt(work, opening, branch, repo):
    return (
        "你是编程会话雷达。把这个 session 此刻正在做的【那一件具体事情】"
        "提炼成一句【不超过 20 字】、以动词开头、聚焦「动作+对象」的中文短语"
        "（按 feature / bugfix / 重构 维度）。\n"
        "硬性要求：\n"
        "1) 必须【概括改写】，不要照抄下面『最初意图/工作单元』的原文，去掉口语、寒暄、/命令 壳；\n"
        "2) 但必须【忠实】——完整保留原文的方向与主被动关系（谁对齐/适配/迁移/推进到谁、"
        "把 A 同步进 B），【严禁反转方向】，【严禁臆造】原文没提到的对象或关系；\n"
        "3) 拿不准方向时，宁可用更直白保守的动宾短语，也不要猜。\n"
        "只输出短语本身，不要引号、不要解释、不要结尾标点。\n"
        f"仓库: {repo or '?'}\n"
        f"参考·最初意图（勿照抄，需提炼，但方向/关系要忠实）: {(opening or '')[:160]}\n"
        f"当前分支: {branch or '?'}\n"
        f"工作类型: {work.get('type', '?')}\n"
        f"参考·工作单元（勿照抄，需提炼）: {work.get('title', '')[:80]}\n"
        f"正在做(in-progress): {work.get('doing', '') or '—'}\n"
        f"近况: {work.get('detail', '') or '—'}\n"
    )


def llm_refine(sid, work, opening, branch, repo, llm):
    """Return a copy of `work` with `summary` replaced by the LLM phrase (source=llm),
    cached by signal fingerprint (lock-guarded). Falls back to the heuristic work
    unchanged when the LLM yields nothing."""
    fp = _fingerprint(sid, work)
    phrase = _cache_get(fp)
    if phrase is None:
        if not llm:
            return work
        phrase = llm(build_llm_prompt(work, opening, branch, repo))
        if not phrase:
            return work
        _cache_set(fp, phrase)
    out = dict(work)
    out["summary"] = phrase
    out["source"] = "llm"
    return out


def _session_id_from_path(p):
    return os.path.splitext(os.path.basename(p))[0]


def _read_jsonl(path):
    objs = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    objs.append(json.loads(ln))
                except Exception:
                    continue
    except OSError:
        pass
    return objs


def _row_from_objs(sid, age, objs, job):
    opening = extract_opening(objs)
    activity, cwd, branch, errored = extract_activity(objs)
    is_bg = job is not None
    bg_state = job["state"] if job else None
    if is_bg and job.get("intent") and not opening:
        opening = _ws(job["intent"])
    if is_bg and not cwd and job.get("cwd"):
        cwd = job["cwd"]
    live = liveness(age, is_bg, bg_state, errored)
    project = project_of(cwd)
    work = summarize_session(objs, job, opening, branch, errored, activity)
    now_text = work["summary"] or activity or (f"[bg:{bg_state}]" if bg_state else "—")
    drift_basis = " ".join(x for x in (work.get("title"), work.get("doing")) if x and x != "—")
    is_stale = is_bg and not cwd and not opening
    return {
        "session_id": sid,
        "short": sid[:8],
        "age_s": round(age, 1),
        "liveness": live,
        "liveness_label": LIVENESS_LABELS.get(live, live),
        "is_bg": is_bg,
        "bg_state": bg_state,
        "project": project,
        "acting_on": _now_other_repo(objs, project),
        "repo": repo_of(cwd),
        "cwd": cwd,
        "worktree": worktree_of(cwd),
        "branch": branch or "—",
        "opening": opening or "—",
        "work": work,
        "now": now_text,
        "errored": errored,
        "drift": compute_drift(opening, drift_basis),
        "is_stale": is_stale,
    }


def _bg_refine(fp, sid, work, opening, branch, repo, llm):
    """Background worker: compute the LLM headline and store it under lock. The
    `_claim_pending(fp)` caller guaranteed exactly one worker owns this fingerprint.
    Any LLM failure is swallowed — the row already shows the heuristic fallback."""
    try:
        phrase = llm(build_llm_prompt(work, opening, branch, repo))
        if phrase:
            _cache_set(fp, phrase)
    except Exception:
        pass
    finally:
        _release_pending(fp)


# Liveness ranked by "how alive / representative" — the merged row borrows the
# liveliest window's summary and status.
_LIVENESS_RANK = {"active": 6, "working": 5, "errored": 4, "blocked": 3,
                  "idle": 2, "dormant": 1, "done": 0, "unknown": 0}


def _work_unit_key(r):
    """The unit-of-work identity for consolidation. A non-generic branch IS the
    work unit (every window on `feat/x` is the same feature). Background jobs and
    generic-branch (main/HEAD/…) sessions have no shared work unit → never merged."""
    br = (r.get("branch") or "").strip()
    if r.get("is_bg") or br.lower() in _GENERIC_BRANCH:
        return None
    return (r.get("project"), br)


def consolidate_work_units(rows):
    """Merge multiple session windows on the SAME work unit (project+branch) into
    one row carrying `session_count`, so N windows on one feature read as one thing
    instead of N near-identical lines. The liveliest, most-recent window represents
    the unit; its edits/commits and drift are unioned. Singletons and bg/generic
    rows pass through unchanged (each gets session_count=1)."""
    groups, order, passthrough = {}, [], []
    for r in rows:
        k = _work_unit_key(r)
        if k is None:
            r["session_count"] = 1
            passthrough.append(r)
            continue
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)
    merged = []
    for k in order:
        grp = groups[k]
        if len(grp) == 1:
            grp[0]["session_count"] = 1
            merged.append(grp[0])
            continue
        primary = min(grp, key=lambda r: (-_LIVENESS_RANK.get(r["liveness"], 0), r["age_s"]))
        m = dict(primary)
        m["session_count"] = len(grp)
        m["merged_shorts"] = [r["short"] for r in grp]
        m["drift"] = any(r["drift"] for r in grp)
        m["age_s"] = min(r["age_s"] for r in grp)
        merged.append(m)
    return passthrough + merged


def build_snapshot(home, window_min=WINDOW_MIN_DEFAULT, now=None,
                   use_llm=False, llm=None, llm_block=False):
    home = os.path.expanduser(home)
    now = now if now is not None else time.time()
    jobs = bg_jobs(home)
    rows, seen = [], set()
    # `projects/*/*.jsonl` is exactly TWO levels deep, so it matches the top-level
    # session transcript `projects/<encoded>/<uuid>.jsonl` but NOT subagent
    # transcripts at `projects/<encoded>/<uuid>/subagents/agent-*.jsonl` (four
    # levels). Subagents are spawned helpers, not user-opened sessions; excluding
    # them is intentional (proven by test_subagent_transcripts_excluded).
    for path in glob.glob(os.path.join(home, "projects", "*", "*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        age = now - mtime
        sid = _session_id_from_path(path)
        short = sid[:8]
        job = jobs.get(short)
        is_bg = job is not None
        # Window filter trims only QUIET FOREGROUND sessions. Every background job
        # (~/.claude/jobs/) is in-scope per SEED ("所有后台任务") and is always
        # surfaced regardless of age or state — even done/dormant ones.
        if window_min and age > window_min * 60 and not is_bg:
            continue
        objs = _read_jsonl(path)
        if not objs:
            continue
        if not is_bg and _is_radar_helper_session(objs):
            continue   # the radar's own `claude -p` summary call — not a real session
        rows.append(_row_from_objs(sid, age, objs, job))
        seen.add(short)
    # Every remaining background job (transcript not in projects/, or none at all)
    # still surfaces — done/blocked/active/unknown/missing-state alike.
    for short, job in jobs.items():
        if short in seen:
            continue
        objs = _read_jsonl(job["link_path"]) if job.get("link_path") else []
        age = (now - os.path.getmtime(job["link_path"])) if (
            job.get("link_path") and os.path.exists(job["link_path"])) else 0.0
        sid = _session_id_from_path(job["link_path"]) if job.get("link_path") else short
        rows.append(_row_from_objs(sid, age, objs, job))
        seen.add(short)
    rows.sort(key=lambda r: r["age_s"])
    # LLM headline pass (opt-in). Distill EVERY informative row — including bg,
    # whose human-authored intent is often a long verbatim sentence that, shown
    # as-is, just echoes the opening (zero distillation value); the LLM condenses
    # it into the concrete action. Only truly info-less stale jobs (no cwd / no
    # intent) are skipped. Cached by signal fingerprint. `llm_block` computes
    # inline (used by --once and tests); otherwise the server schedules misses in
    # the background pool so /api/state NEVER blocks on the LLM — a later manual
    # refresh picks up the cached result.
    if use_llm and llm:
        for r in rows:
            if r["is_stale"]:
                continue
            fp = _fingerprint(r["session_id"], r["work"])
            cached = _cache_get(fp)
            if cached is not None:                       # already summarized → use it
                out = dict(r["work"])
                out["summary"], out["source"] = cached, "llm"
                r["work"], r["now"] = out, cached
            elif llm_block:                              # --once / tests: compute inline
                r["work"] = llm_refine(r["session_id"], r["work"], r["opening"],
                                       r["branch"], r["project"], llm)
                r["now"] = r["work"]["summary"]
            elif _claim_pending(fp):                     # server: schedule once, never block
                _llm_pool().submit(_bg_refine, fp, r["session_id"], dict(r["work"]),
                                   r["opening"], r["branch"], r["project"], llm)
    # Group the metadata-bearing rows by their MAIN git repo; collapse the
    # no-metadata stale jobs into a single count.
    stale = [r for r in rows if r["is_stale"]]
    # Consolidate N windows on the same work unit (project+branch) into one row.
    visible = consolidate_work_units([r for r in rows if not r["is_stale"]])
    by_project = {}
    for r in visible:
        by_project.setdefault(r["project"], []).append(r)
    projects = [{"project": p,
                 "sessions": sorted(sess, key=lambda r: r["age_s"]),
                 "count": sum(s.get("session_count", 1) for s in sess),
                 "units": len(sess)}
                for p, sess in by_project.items()]
    # Freshest project first (smallest min-age in the group).
    projects.sort(key=lambda g: min(s["age_s"] for s in g["sessions"]))
    counts = {
        "total": len(rows),
        "bg": sum(1 for r in rows if r["is_bg"]),
        "errored": sum(1 for r in rows if r["errored"]),
        "drift": sum(1 for r in rows if r["drift"]),
        "projects": len(projects),
        "stale_unknown": len(stale),
    }
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "claude_home": home,
        "window_min": window_min,
        "counts": counts,
        "projects": projects,
        "stale_unknown": len(stale),
        "sessions": rows,
    }


HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Session Radar</title>
<style>
:root{--bg:#0f1115;--fg:#e6e6e6;--mut:#8a8f98;--line:#222733;--card:#161a22;
--green:#3fb950;--yellow:#d29922;--gray:#6e7681;--red:#f85149;--orange:#db8a3a;--blue:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;
gap:16px;align-items:baseline;flex-wrap:wrap}
h1{font-size:16px;margin:0}.sub{color:var(--mut);font-size:12px}
main{padding:14px 20px}table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.live{white-space:nowrap}.bg{color:var(--blue)}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}
.repo{font-weight:600}.br{color:var(--mut);font-family:ui-monospace,Menlo,monospace;font-size:12px}
.drift{color:var(--orange);font-weight:600}.flow{color:var(--mut)}
.now{color:var(--fg)}.err td{background:rgba(248,81,73,.06)}
.pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;border:1px solid var(--line)}
.clip{max-width:38ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:inline-block}
tr.grp td{background:#11161f;border-bottom:1px solid var(--line);padding-top:14px}
.grp .pname{font-weight:700;font-size:13px;color:var(--fg)}
.grp .pcount{color:var(--mut);font-size:12px;margin-left:8px}
tr.stale td{color:var(--mut);font-size:12px;font-style:italic;background:transparent}
.act{display:inline-block;margin-left:8px;color:var(--blue);font-size:11px;font-weight:600;
white-space:nowrap}
.wtype{margin-right:6px}.wsum{font-weight:600}
.wcount{margin-right:6px;color:var(--blue);border-color:var(--blue)}
.wprog{color:var(--mut);margin-left:6px;font-size:12px}
.wdetail{color:var(--mut);font-size:11px;margin-top:2px}
.wsrc{color:var(--mut);font-size:10px;margin-left:6px;opacity:.7}
button#refresh{margin-left:auto;background:var(--card);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer}
button#refresh:hover{border-color:var(--blue)}
</style></head><body>
<header><h1>📡 Session Radar</h1>
<span class="sub" id="meta">loading…</span>
<button id="refresh" title="重新扫描快照（含 LLM 摘要，按需刷新）">🔄 刷新</button></header>
<main><table><thead><tr>
<th>Live</th><th>BG</th><th>Worktree</th><th>Branch</th>
<th>Opening → 工作单元（在做什么）</th><th>Drift</th></tr></thead>
<tbody id="rows"></tbody></table></main>
<script>
function esc(s){return (s==null?"":""+s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function grpHead(g){
  var label = (g.units && g.units < g.count)
    ? g.units+' 单元 · '+g.count+' sessions'
    : g.count+' session'+(g.count>1?'s':'');
  return '<tr class="grp"><td colspan="6">'
    +'<span class="pname">📁 '+esc(g.project)+'</span>'
    +'<span class="pcount">'+label+'</span></td></tr>';
}
function workCell(s){
  var w = s.work || {};
  var badge = w.icon ? '<span class="pill wtype">'+esc(w.icon)+' '+esc(w.type)+'</span>' : '';
  var cnt = (s.session_count>1)
    ? '<span class="pill wcount" title="'+s.session_count+' 个 session 在做同一件事，已合并为一个工作单元">⋃ '+s.session_count+'</span>' : '';
  var sum = '<span class="wsum clip" title="'+esc(s.now||w.summary||'')+'">'+esc(s.now||w.summary||'—')+'</span>';
  var prog = w.progress ? '<span class="wprog">'+esc(w.progress)+'</span>' : '';
  var src = (w.source==='llm') ? '<span class="wsrc" title="claude 生成">✨</span>' : '';
  var det = w.detail ? '<div class="wdetail mono">'+esc(w.detail)+'</div>' : '';
  return badge+cnt+sum+prog+src+det;
}
function row(s){
  var wt = s.worktree ? '<span class="br">⟨wt:'+esc(s.worktree)+'⟩</span>' : '<span class="br">—</span>';
  var act = s.acting_on ? '<span class="act" title="current command operates on another repo">↗ '+esc(s.acting_on)+'</span>' : '';
  var drift = s.drift ? '<span class="drift">⤳ DRIFT</span>' : '';
  return '<tr class="'+(s.errored?'err':'')+'">'
    +'<td class="live">'+esc(s.liveness_label)+'</td>'
    +'<td class="bg">'+(s.is_bg?'🤖':'')+'</td>'
    +'<td>'+wt+'</td>'
    +'<td class="br">'+esc(s.branch)+'</td>'
    +'<td><span class="clip" title="'+esc(s.opening)+'">'+esc(s.opening)+'</span>'
      +'<span class="flow"> → </span>'+workCell(s)+act+'</td>'
    +'<td>'+drift+'</td></tr>';
}
function load(){
  fetch("/api/state").then(r=>r.json()).then(d=>{
    var c=d.counts||{};
    document.getElementById("meta").textContent=
      (c.total||0)+" sessions · "+(c.projects||0)+" projects · "+(c.bg||0)+" bg · "
      +(c.errored||0)+" errored · "+(c.drift||0)+" drifted · "+esc(d.generated_at);
    var html="";
    (d.projects||[]).forEach(function(g){ html += grpHead(g) + g.sessions.map(row).join(""); });
    if((d.stale_unknown||0)>0){
      html += '<tr class="stale"><td colspan="6">+ '+d.stale_unknown
        +' 无元数据的旧后台任务（jobs/ 里无 state.json，已折叠）</td></tr>';
    }
    document.getElementById("rows").innerHTML=html;
  }).catch(e=>{document.getElementById("meta").textContent="error: "+e;});
}
document.getElementById("refresh").onclick=load; load();
</script></body></html>"""


def render_html():
    return HTML


def render_table(snap):
    c = snap.get("counts", {})
    out = [f"📡 Session Radar — {c.get('total',0)} sessions in {c.get('projects',0)} projects "
           f"({c.get('bg',0)} bg, {c.get('errored',0)} errored, {c.get('drift',0)} drifted)"
           f"  @ {snap.get('generated_at','')}"]
    for g in snap.get("projects", []):
        out.append("")
        out.append(f"📁 {g['project']}  ({g['count']})")
        out.append("  " + "-" * 100)
        out.append(f"  {'LIVE':<11} {'BG':<3} {'WORKTREE':<22} {'BRANCH':<20} OPENING → NOW")
        for r in g.get("sessions", []):
            bg = "🤖" if r.get("is_bg") else "  "
            wt = (r.get("worktree") or "—")[:21]
            br = (r.get("branch") or "—")[:19]
            act = f"  ↗{r['acting_on']}" if r.get("acting_on") else ""
            drift = "  ⤳DRIFT" if r.get("drift") else ""
            w = r.get("work") or {}
            now = w.get("summary") or r.get("now") or "—"
            if w.get("progress"):
                now += f" [{w['progress']}]"
            cnt = f"⋃{r['session_count']} " if r.get("session_count", 1) > 1 else ""
            out.append(f"  {r.get('liveness_label',''):<11} {bg:<3} {wt:<22} {br:<20} "
                       f"{(r.get('opening') or '—')[:30]} → {cnt}{now[:50]}{act}{drift}")
    stale = snap.get("stale_unknown", 0)
    if stale:
        out.append("")
        out.append(f"+ {stale} 无元数据的旧后台任务（jobs/ 里无 state.json，已折叠）")
    return "\n".join(out)


class _Handler(BaseHTTPRequestHandler):
    home = os.path.expanduser("~/.claude")
    window_min = WINDOW_MIN_DEFAULT
    use_llm = False
    llm = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, "ok", "text/plain; charset=utf-8")
        if self.path.startswith("/api/state"):
            snap = build_snapshot(self.home, self.window_min,
                                  use_llm=self.use_llm, llm=self.llm, llm_block=False)
            return self._send(200, json.dumps(snap, ensure_ascii=False),
                              "application/json; charset=utf-8")
        if self.path == "/" or self.path.startswith("/index"):
            return self._send(200, render_html(), "text/html; charset=utf-8")
        return self._send(404, "not found", "text/plain; charset=utf-8")


def serve(home, port=DEFAULT_PORT, window_min=WINDOW_MIN_DEFAULT, max_tries=20,
          use_llm=False, llm=None):
    """Bind 127.0.0.1:port, polling upward until a free port is found."""
    _Handler.home = os.path.expanduser(home)
    _Handler.window_min = window_min
    _Handler.use_llm = use_llm
    # staticmethod: llm is a plain function; assigned as a bare class attribute it
    # would become a BOUND METHOD on `self.llm` access (Python descriptor protocol),
    # injecting `self` as a phantom first arg → every LLM call raises TypeError and
    # the snapshot silently falls back to heuristics. staticmethod stops the binding.
    _Handler.llm = staticmethod(llm) if llm else None
    last_err = None
    for i in range(max_tries):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port + i), _Handler)
        except OSError as e:
            last_err = e
            continue
        actual = port + i
        mode = "LLM" if (use_llm and llm) else "heuristic"
        print(f"📡 session-radar → http://127.0.0.1:{actual}  "
              f"(claude_home={_Handler.home}, summaries={mode})")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
        return actual
    raise SystemExit(f"no free port in [{port},{port+max_tries}): {last_err}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Session Radar — every local Claude session at a glance")
    ap.add_argument("--claude-home", default=os.path.expanduser("~/.claude"),
                    help="root of ~/.claude (projects/ + jobs/)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--window-min", type=int, default=WINDOW_MIN_DEFAULT,
                    help="recency lens for FOREGROUND sessions: hide those untouched for > N min "
                         "(0 = no filter, show all). Background jobs are ALWAYS shown.")
    ap.add_argument("--once", action="store_true", help="print a terminal table and exit")
    ap.add_argument("--json", action="store_true", help="print snapshot JSON and exit")
    ap.add_argument("--no-llm", action="store_true",
                    help="disable the claude-CLI work-unit summary (use heuristic only)")
    ap.add_argument("--llm-model", default=DEFAULT_LLM_MODEL,
                    help="claude model for the summary (default: %(default)s)")
    args = ap.parse_args(argv)
    use_llm = (not args.no_llm) and claude_available()
    llm = make_claude_llm(args.llm_model) if use_llm else None
    if args.json:
        print(json.dumps(build_snapshot(args.claude_home, args.window_min,
                                        use_llm=use_llm, llm=llm, llm_block=True),
                         ensure_ascii=False, indent=2))
        return 0
    if args.once:
        print(render_table(build_snapshot(args.claude_home, args.window_min,
                                          use_llm=use_llm, llm=llm, llm_block=True)))
        return 0
    return serve(args.claude_home, args.port, args.window_min, use_llm=use_llm, llm=llm)


if __name__ == "__main__":
    raise SystemExit(main())
