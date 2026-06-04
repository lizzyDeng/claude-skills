# Session Radar Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a stdlib-only Python dashboard that scans every local Claude session (foreground transcripts + background daemon jobs) and shows, per session, its liveness, the repo/worktree/branch it is acting on *right now*, and the drift between its opening intent and its current action.

**Architecture:** One self-contained module `skills/session-radar/session_dashboard.py` reusing the stdlib web-shell shape of `skills/forge/forge_dashboard.py` (`ThreadingHTTPServer` + `/api/state` JSON + client-rendered HTML, plus a `--once` terminal table). Data comes from `~/.claude/projects/*/*.jsonl` (foreground sessions) and `~/.claude/jobs/<short>/state.json` (background jobs). Pure heuristics, no LLM. The three SEED hard-problems are solved as discrete, unit-tested functions: (1) `strip_command_shell` removes the `<command-*>` / `<local-command-caveat>` shell so the opening shows real human intent; (2) `liveness` consults the authoritative job `state` field for background jobs instead of trusting transcript mtime; (3) `extract_activity` classifies `API Error:`-style tails as a distinct `errored` state instead of reporting them as work.

**Tech Stack:** Python 3 stdlib only — `argparse`, `glob`, `json`, `os`, `re`, `socket`, `time`, `datetime`, `http.server`, `urllib`. No third-party deps, no build step. Tests use `unittest` (runnable via `python3 -m unittest` or `pytest`). E2E is a pure-Python git/HTTP runner per the KNOWLEDGE.md "纯 Python skill 的 E2E" recipe.

---

## Requirements → Acceptance Criteria → E2E mapping (the coverage contract)

| ID | Requirement (from SEED) | Observable AC | E2E scenario turn(s) |
|----|-------------------------|---------------|----------------------|
| **P0-1** | Liveness for ALL sessions incl. background; bg liveness must NOT rely on mtime alone (hard problem #2); error tails ≠ work (hard problem #3) | A bg job whose `state.json.state` is `active` but whose transcript mtime is older than the idle threshold renders liveness `working` (not `dormant`). A bg job with no/unrecognized state renders `unknown` (never `active` off a fabricated age). A session whose tail is `API Error: …` renders liveness `errored` and `errored=true`. | "bg active-but-stale → working not dormant"; "stateless bg → unknown not active"; "errored tail → liveness errored, errored flag set" |
| **P0-2** | Current repo / worktree / branch | Each session exposes `repo`, `worktree`, `branch` derived from the LATEST transcript event (current cwd/gitBranch), with worktree extracted from a `.claude/worktrees/<name>` cwd. | "current repo/branch from tail (drifted session in plain repo)"; "worktree session ROW exposes worktree==`session-radar`, repo==`claude-skills ⟨wt:session-radar⟩`, branch from tail" |
| **P0-3** | `opening intent → 当前实际动作` drift; strip command shell (hard problem #1) | For a `/fastship`-started session the `opening` equals the real human ARGUMENTS, never `<command-message>…`. When the current action's tokens diverge from the opening, `drift=true`. | "command-shell session → opening == real intent, no XML"; "diverged session → drift flagged" |
| **P0-4** | Scope = all sessions incl. all background jobs | `/api/state` `sessions` includes foreground sessions AND every background job — alive, blocked, AND done/stateless — even one with no transcript in `projects/`. bg rows carry `is_bg=true`; counts report `bg`. | "fg + all 4 bg kinds present, counts.bg==4"; "done bg (no transcript) → liveness `done` + intent opening"; "blocked bg → `blocked`"; "stateless bg → `unknown`" |
| **P0-5** | Reuse stdlib web shell; `--once` terminal table free | Server answers `/healthz` 200, `/api/state` returns the snapshot JSON, `/` serves HTML that fetches `/api/state`; `--once` prints a terminal table; `--json` prints snapshot JSON. | "healthz 200"; "/api/state shape"; "/ serves HTML referencing /api/state"; "--json subprocess emits valid snapshot" |

Every P0 has an observable AC; every AC maps to ≥1 E2E turn that checks a business result (JSON field value, derived state, HTTP body), not a smoke signal. The E2E runner (Task 8) builds a fake `~/.claude` home whose fixtures deterministically trigger each AC.

---

## File Structure

- **Create** `skills/session-radar/session_dashboard.py` — the entire module (data extraction + heuristics + HTTP server + CLI). One file, ~360 lines, mirroring `skills/forge/forge_dashboard.py`'s self-contained shape.
- **Create** `tests/session_radar/test_session_dashboard.py` — `unittest` unit tests over a fake `~/.claude` home built in `tempfile.mkdtemp()`.
- **Create** `tests/session_radar/session_radar_e2e_runner.py` — pure-Python E2E runner (filename matches `e2e[_-]?runner`). Builds a fake home, boots the real server via subprocess, hits endpoints with `urllib`, emits nested+flat turn schema.
- **Create** `skills/session-radar/SKILL.md` — short skill doc + usage.
- **Create** `skills/session-radar/session-radar` — bash launcher (mirrors `skills/forge/forge-dashboard`).
- **Create** `.claude/fastship.project.json` — pins this deliverable's E2E `result_path`/`runner_command`/`gate_command`/`min_turns` so the runner writes to a repo-local path (not shared `/tmp`) and the gate/validator read the same path.
- **Modify** `.gitignore` — ignore the generated `tmp/` E2E result.
- **Delete** `_session_radar_seed/` — SEED.md says it is not a deliverable; remove before ship.

Import convention (matches `tests/forge/test_forge_dashboard.py`): tests load the module by file path via `importlib.util.spec_from_file_location`, so the hyphen in the dir name is irrelevant.

---

## Task 1: Scaffold module + `strip_command_shell` (hard problem #1, part A)

**Files:**
- Create: `skills/session-radar/session_dashboard.py`
- Test: `tests/session_radar/test_session_dashboard.py`

- [ ] **Step 1: Write the failing test**

Create `tests/session_radar/test_session_dashboard.py`:

```python
import json, os, tempfile, time, unittest, importlib.util

MOD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "skills", "session-radar", "session_dashboard.py")
spec = importlib.util.spec_from_file_location("session_dashboard", MOD)
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)


class StripCommandShellTest(unittest.TestCase):
    def test_extracts_real_args_from_fastship_shell(self):
        raw = ("<command-message>fastship</command-message>\n"
               "<command-name>/fastship</command-name>\n"
               "<command-args>做 session 维度可视化雷达</command-args>")
        self.assertEqual(sd.strip_command_shell(raw), "做 session 维度可视化雷达")

    def test_drops_local_command_caveat_block(self):
        raw = ("<local-command-caveat>Caveat: messages below were generated "
               "while running local commands. DO NOT respond.</local-command-caveat>")
        self.assertEqual(sd.strip_command_shell(raw), "")

    def test_plain_human_prompt_passes_through_untouched(self):
        self.assertEqual(sd.strip_command_shell("Please add a retry to the client"),
                         "Please add a retry to the client")

    def test_plain_prompt_with_angle_brackets_not_mangled(self):
        # no command-shell tags -> leave content alone (only whitespace-normalize)
        self.assertEqual(sd.strip_command_shell("fix List<Item> generic   parse"),
                         "fix List<Item> generic parse")

    def test_non_string_returns_empty(self):
        self.assertEqual(sd.strip_command_shell(None), "")
        self.assertEqual(sd.strip_command_shell({"a": 1}), "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v` (from repo root)
Expected: FAIL — collection error because `session_dashboard.py` does not exist yet.

> **Test-runner note (resolved during grill):** use `pytest`, not dotted `python3 -m unittest tests.session_radar.…`. `tests/` has no `__init__.py`, so dotted unittest module paths don't import; pytest discovers by file path. pytest is also a recognized `is_test_cmd` python pattern (`\bpytest\b`), and prints `N passed`, so the hook sets `test_passed` for Step 3.1. The `unittest.TestCase` classes run unchanged under pytest.

- [ ] **Step 3: Write minimal implementation**

Create `skills/session-radar/session_dashboard.py` with the module header and the function:

```python
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
import json
import os
import re
import socket
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v`
Expected: PASS (5 tests in `StripCommandShellTest`).

- [ ] **Step 5: Commit**

```bash
git add skills/session-radar/session_dashboard.py tests/session_radar/test_session_dashboard.py
git commit -m "feat(session-radar): strip command-shell to recover real opening intent"
```

---

## Task 2: `extract_opening` (hard problem #1, part B)

**Files:**
- Modify: `skills/session-radar/session_dashboard.py`
- Test: `tests/session_radar/test_session_dashboard.py`

- [ ] **Step 1: Write the failing test** — append to the test file:

```python
class ExtractOpeningTest(unittest.TestCase):
    def test_skips_caveat_returns_real_args(self):
        objs = [
            {"type": "user", "message": {"role": "user",
                "content": "<local-command-caveat>Caveat: ...</local-command-caveat>"}},
            {"type": "user", "message": {"role": "user",
                "content": "<command-message>fastship</command-message>"
                           "<command-args>build the radar</command-args>"}},
        ]
        self.assertEqual(sd.extract_opening(objs), "build the radar")

    def test_skips_tool_result_user_turns(self):
        objs = [
            {"type": "user", "message": {"role": "user",
                "content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "user", "message": {"role": "user",
                "content": [{"type": "text", "text": "real first ask"}]}},
        ]
        self.assertEqual(sd.extract_opening(objs), "real first ask")

    def test_returns_empty_when_no_human_turn(self):
        objs = [{"type": "assistant", "message": {"role": "assistant",
                 "content": [{"type": "text", "text": "hi"}]}}]
        self.assertEqual(sd.extract_opening(objs), "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v -k ExtractOpening`
Expected: FAIL — `AttributeError: module has no attribute 'extract_opening'`.

- [ ] **Step 3: Write minimal implementation** — append to `session_dashboard.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v`
Expected: PASS (all tests so far).

- [ ] **Step 5: Commit**

```bash
git add skills/session-radar/session_dashboard.py tests/session_radar/test_session_dashboard.py
git commit -m "feat(session-radar): extract first genuine human opening prompt"
```

---

## Task 3: `extract_activity` + error classification (hard problem #3)

**Files:**
- Modify: `skills/session-radar/session_dashboard.py`
- Test: `tests/session_radar/test_session_dashboard.py`

- [ ] **Step 1: Write the failing test** — append:

```python
class ExtractActivityTest(unittest.TestCase):
    def test_latest_tool_use_becomes_activity_with_cwd_branch(self):
        objs = [
            {"type": "assistant", "cwd": "/repo", "gitBranch": "main",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "older"}]}},
            {"type": "assistant", "cwd": "/repo/x", "gitBranch": "feat/y",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Edit", "input": {"file_path": "/repo/x/a.py"}}]}},
        ]
        activity, cwd, branch, errored = sd.extract_activity(objs)
        self.assertIn("Edit", activity)
        self.assertIn("a.py", activity)
        self.assertEqual(cwd, "/repo/x")
        self.assertEqual(branch, "feat/y")
        self.assertFalse(errored)

    def test_api_error_tail_flags_errored(self):
        objs = [
            {"type": "assistant", "cwd": "/repo", "gitBranch": "main",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "API Error: 529 overloaded_error"}]}},
        ]
        activity, cwd, branch, errored = sd.extract_activity(objs)
        self.assertTrue(errored)
        self.assertTrue(activity.startswith("⚠"))

    def test_no_assistant_returns_empty_activity(self):
        objs = [{"type": "user", "cwd": "/repo", "message": {"role": "user", "content": "hi"}}]
        activity, cwd, branch, errored = sd.extract_activity(objs)
        self.assertEqual(activity, "")
        self.assertEqual(cwd, "/repo")
        self.assertFalse(errored)


class IsErrorTextTest(unittest.TestCase):
    def test_detects_common_error_prefixes(self):
        for s in ["API Error: overloaded", "  request failed: timeout",
                  "Error: boom", "HTTP 500 server error", "Rate limit exceeded"]:
            self.assertTrue(sd.is_error_text(s), s)

    def test_normal_text_not_error(self):
        self.assertFalse(sd.is_error_text("Editing the error handler in client.py"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v -k "ExtractActivity or IsErrorText"`
Expected: FAIL — `extract_activity` / `is_error_text` not defined.

- [ ] **Step 3: Write minimal implementation** — append:

```python
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
    """Walk the transcript tail: latest assistant action + latest cwd/branch.

    Returns (activity, cwd, branch, errored). An error tail yields a '⚠ …'
    activity and errored=True so liveness can mark it errored, not 'working'.
    """
    cwd = branch = None
    activity = None
    errored = False
    for o in reversed(objs):
        if cwd is None and o.get("cwd"):
            cwd, branch = o.get("cwd"), o.get("gitBranch")
        if activity is None and o.get("type") == "assistant":
            text, tools = _assistant_summary((o.get("message") or {}).get("content"))
            if tools:
                activity = " · ".join(tools[:3])
            elif text:
                if is_error_text(text):
                    errored = True
                    activity = "⚠ " + _ws(text)[:80]
                else:
                    activity = _ws(text)[:90]
        if activity is not None and cwd is not None:
            break
    return (activity or ""), cwd, branch, errored
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/session-radar/session_dashboard.py tests/session_radar/test_session_dashboard.py
git commit -m "feat(session-radar): tail activity extraction with error-state classification"
```

---

## Task 4: `bg_jobs` reader + `liveness` (hard problem #2)

**Files:**
- Modify: `skills/session-radar/session_dashboard.py`
- Test: `tests/session_radar/test_session_dashboard.py`

- [ ] **Step 1: Write the failing test** — append:

```python
def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


class BgJobsTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def test_reads_state_intent_cwd_and_tolerates_missing(self):
        _write(os.path.join(self.home, "jobs", "32ea05ca", "state.json"),
               {"state": "blocked", "intent": "review F4", "cwd": "/x",
                "updatedAt": "2026-06-03T09:07:17Z",
                "linkScanPath": "/p/32ea05ca-aaa.jsonl"})
        os.makedirs(os.path.join(self.home, "jobs", "06d739b8"))  # dir, no state.json
        jobs = sd.bg_jobs(self.home)
        self.assertEqual(jobs["32ea05ca"]["state"], "blocked")
        self.assertEqual(jobs["32ea05ca"]["intent"], "review F4")
        self.assertEqual(jobs["32ea05ca"]["cwd"], "/x")
        self.assertEqual(jobs["32ea05ca"]["link_path"], "/p/32ea05ca-aaa.jsonl")
        self.assertIn("06d739b8", jobs)          # still recognized as a bg job
        self.assertIsNone(jobs["06d739b8"]["state"])

    def test_missing_jobs_dir_returns_empty(self):
        self.assertEqual(sd.bg_jobs(os.path.join(self.home, "nope")), {})


class LivenessTest(unittest.TestCase):
    def test_foreground_uses_mtime_buckets(self):
        self.assertEqual(sd.liveness(10), "active")
        self.assertEqual(sd.liveness(200), "idle")
        self.assertEqual(sd.liveness(9999), "dormant")

    def test_bg_active_overrides_stale_mtime(self):
        # transcript 1 hour idle, but the job says it is active -> working, not dormant
        self.assertEqual(sd.liveness(3600, is_bg=True, bg_state="active"), "working")

    def test_bg_blocked_is_distinct_alive_state(self):
        self.assertEqual(sd.liveness(3600, is_bg=True, bg_state="blocked"), "blocked")

    def test_bg_done_is_done(self):
        self.assertEqual(sd.liveness(5, is_bg=True, bg_state="done"), "done")

    def test_errored_beats_everything(self):
        self.assertEqual(sd.liveness(5, is_bg=True, bg_state="active", errored=True), "errored")

    def test_unknown_bg_state_is_unknown_never_mtime_active(self):
        # a fresh-mtime bg job with an unrecognized state must NOT read 'active'
        self.assertEqual(sd.liveness(10, is_bg=True, bg_state="weird"), "unknown")

    def test_missing_bg_state_is_unknown(self):
        self.assertEqual(sd.liveness(0.0, is_bg=True, bg_state=None), "unknown")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v -k "BgJobs or Liveness"`
Expected: FAIL — `bg_jobs` / `liveness` not defined.

- [ ] **Step 3: Write minimal implementation** — append:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/session-radar/session_dashboard.py tests/session_radar/test_session_dashboard.py
git commit -m "feat(session-radar): bg-job state reader + liveness with bg-state authority"
```

---

## Task 5: `repo_of` / `worktree_of` + `compute_drift`

**Files:**
- Modify: `skills/session-radar/session_dashboard.py`
- Test: `tests/session_radar/test_session_dashboard.py`

- [ ] **Step 1: Write the failing test** — append:

```python
class RepoWorktreeTest(unittest.TestCase):
    def test_worktree_from_claude_worktrees_path(self):
        cwd = "/Users/me/works/claude-skills/.claude/worktrees/session-radar"
        self.assertEqual(sd.worktree_of(cwd), "session-radar")
        self.assertEqual(sd.repo_of(cwd), "claude-skills ⟨wt:session-radar⟩")

    def test_plain_repo_uses_basename(self):
        self.assertIsNone(sd.worktree_of("/Users/me/works/aifriends"))
        self.assertEqual(sd.repo_of("/Users/me/works/aifriends"), "aifriends")

    def test_none_cwd(self):
        self.assertIsNone(sd.worktree_of(None))
        self.assertEqual(sd.repo_of(None), "—")


class DriftTest(unittest.TestCase):
    def test_diverged_opening_and_now_is_drift(self):
        self.assertTrue(sd.compute_drift("build a session radar dashboard",
                                         "Bash(git push origin pr-branch) open pull request"))

    def test_aligned_opening_and_now_no_drift(self):
        self.assertFalse(sd.compute_drift("fix the retry backoff in client",
                                          "Edit(client.py) retry backoff jitter"))

    def test_empty_side_is_not_drift(self):
        self.assertFalse(sd.compute_drift("", "anything"))
        self.assertFalse(sd.compute_drift("anything", ""))

    def test_cjk_opening_participates_in_drift(self):  # the seed's own opening is Chinese
        # diverged: Chinese "build the radar" intent vs opening-a-PR action -> drift
        self.assertTrue(sd.compute_drift("做 session 维度可视化雷达", "Bash(git push) open PR 提交"))
        # aligned: shared Chinese tokens -> no drift
        self.assertFalse(sd.compute_drift("修复 重试 逻辑 backoff", "Edit 修复 重试 逻辑 jitter"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v -k "RepoWorktree or Drift"`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Write minimal implementation** — append:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/session-radar/session_dashboard.py tests/session_radar/test_session_dashboard.py
git commit -m "feat(session-radar): repo/worktree derivation + opening→now drift heuristic"
```

---

## Task 6: `build_snapshot` (aggregation over fake home; P0-1..P0-4)

**Files:**
- Modify: `skills/session-radar/session_dashboard.py`
- Test: `tests/session_radar/test_session_dashboard.py`

- [ ] **Step 1: Write the failing test** — append. This test builds a fake `~/.claude` home exercising every P0 AC:

```python
def _jsonl(path, objs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")


class BuildSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        proj = os.path.join(self.home, "projects", "-Users-me-works-claude-skills")
        # FG session in a worktree, /fastship opening, current action = open PR (drift)
        self._fg = "aaaaaaaa-1111-2222-3333-444444444444"
        _jsonl(os.path.join(proj, self._fg + ".jsonl"), [
            {"type": "user", "cwd": "/Users/me/works/claude-skills/.claude/worktrees/session-radar",
             "gitBranch": "feat/session-radar-dashboard",
             "message": {"role": "user",
                "content": "<command-message>fastship</command-message>"
                           "<command-args>build the session radar dashboard</command-args>"}},
            {"type": "assistant",
             "cwd": "/Users/me/works/aifriends", "gitBranch": "fix/provider",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Bash", "input": {"command": "git push origin pr"}}]}},
        ])
        # BG job: transcript is 1h stale, but job state = active -> must be 'working'
        self._bg = "32ea05ca-71ef-40de-8b31-b1c929992902"
        bgp = os.path.join(proj, self._bg + ".jsonl")
        _jsonl(bgp, [
            {"type": "user", "cwd": "/Users/me/works/aifriends", "gitBranch": "main",
             "message": {"role": "user", "content": "review F4 alignment"}},
            {"type": "assistant", "cwd": "/Users/me/works/aifriends", "gitBranch": "main",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Read", "input": {"file_path": "sdk.py"}}]}},
        ])
        old = time.time() - 3600
        os.utime(bgp, (old, old))
        _write(os.path.join(self.home, "jobs", "32ea05ca", "state.json"),
               {"state": "active", "intent": "review F4 alignment", "cwd": "/Users/me/works/aifriends",
                "updatedAt": "2026-06-03T09:00:00Z", "linkScanPath": bgp})
        # Errored FG session: tail is an API error
        self._err = "bbbbbbbb-5555-6666-7777-888888888888"
        _jsonl(os.path.join(proj, self._err + ".jsonl"), [
            {"type": "user", "cwd": "/repo", "gitBranch": "main",
             "message": {"role": "user", "content": "do the thing"}},
            {"type": "assistant", "cwd": "/repo", "gitBranch": "main",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "API Error: 529 overloaded_error"}]}},
        ])
        # FG session whose CURRENT action is inside a .claude/worktrees/<name> cwd
        self._wt = "dddddddd-aaaa-bbbb-cccc-dddddddddddd"
        _jsonl(os.path.join(proj, self._wt + ".jsonl"), [
            {"type": "user", "cwd": "/Users/me/works/claude-skills",
             "gitBranch": "main", "message": {"role": "user", "content": "ship the radar"}},
            {"type": "assistant",
             "cwd": "/Users/me/works/claude-skills/.claude/worktrees/session-radar",
             "gitBranch": "feat/session-radar-dashboard",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Edit", "input": {"file_path": "session_dashboard.py"}}]}},
        ])
        # DONE background job with NO live transcript in projects/ — must still surface
        self._done = "cafe1234"
        _write(os.path.join(self.home, "jobs", self._done, "state.json"),
               {"state": "done", "intent": "earlier finished job",
                "cwd": "/Users/me/works/other", "updatedAt": "2026-06-01T00:00:00Z"})
        # BLOCKED background job (awaiting input) — alive, distinct from done
        self._blk = "beef5678"
        _write(os.path.join(self.home, "jobs", self._blk, "state.json"),
               {"state": "blocked", "intent": "waiting on permission",
                "cwd": "/Users/me/works/other"})
        # STATELESS background job: dir exists, NO state.json — liveness must be 'unknown',
        # never 'active' (no transcript -> fabricated age must not leak through)
        self._nost = "0d0d0d0d"
        os.makedirs(os.path.join(self.home, "jobs", self._nost))

    def _by_short(self, snap, short):
        return next(s for s in snap["sessions"] if s["short"] == short)

    def test_top_level_shape(self):
        snap = sd.build_snapshot(self.home, window_min=120)
        for k in ("generated_at", "claude_home", "counts", "sessions", "window_min"):
            self.assertIn(k, snap)
        # 2 plain fg + 1 worktree fg + 4 bg (active, done, blocked, stateless) = 7
        self.assertEqual(snap["counts"]["total"], 7)
        self.assertEqual(snap["counts"]["bg"], 4)

    def test_p0_2_worktree_row_exposes_worktree_and_repo(self):  # P0-2
        snap = sd.build_snapshot(self.home, window_min=120)
        wt = self._by_short(snap, "dddddddd")
        self.assertEqual(wt["worktree"], "session-radar")
        self.assertEqual(wt["repo"], "claude-skills ⟨wt:session-radar⟩")
        self.assertEqual(wt["branch"], "feat/session-radar-dashboard")

    def test_p0_4_done_bg_job_without_transcript_still_surfaces(self):  # P0-4
        snap = sd.build_snapshot(self.home, window_min=120)
        done = self._by_short(snap, "cafe1234")
        self.assertTrue(done["is_bg"])
        self.assertEqual(done["liveness"], "done")
        self.assertEqual(done["opening"], "earlier finished job")  # from job intent

    def test_p0_4_blocked_bg_job_surfaces_as_blocked(self):  # P0-4 + P0-1
        snap = sd.build_snapshot(self.home, window_min=120)
        blk = self._by_short(snap, "beef5678")
        self.assertTrue(blk["is_bg"])
        self.assertEqual(blk["liveness"], "blocked")

    def test_p0_1_stateless_bg_job_is_unknown_not_active(self):  # P0-1 hard problem #2
        snap = sd.build_snapshot(self.home, window_min=120)
        nost = self._by_short(snap, "0d0d0d0d")
        self.assertTrue(nost["is_bg"])
        self.assertEqual(nost["liveness"], "unknown")  # NOT 'active' off a fabricated age

    def test_window_min_zero_shows_all_foreground_including_old(self):
        # The window is a recency lens, not a scope cap: --window-min 0 disables it.
        proj = os.path.join(self.home, "projects", "-Users-me-works-claude-skills")
        oldp = os.path.join(proj, "eeeeeeee-old.jsonl")
        _jsonl(oldp, [{"type": "user", "cwd": "/old/repo", "gitBranch": "main",
                       "message": {"role": "user", "content": "ancient task"}},
                      {"type": "assistant", "cwd": "/old/repo", "gitBranch": "main",
                       "message": {"role": "assistant", "content": [
                           {"type": "text", "text": "done long ago"}]}}])
        old = time.time() - 30 * 24 * 3600
        os.utime(oldp, (old, old))
        hidden = {s["short"] for s in sd.build_snapshot(self.home, window_min=120)["sessions"]}
        self.assertNotIn("eeeeeeee", hidden)                       # default window hides it
        shown = {s["short"] for s in sd.build_snapshot(self.home, window_min=0)["sessions"]}
        self.assertIn("eeeeeeee", shown)                           # window 0 = full scope

    def test_subagent_transcripts_excluded(self):
        # A subagent transcript lives 4 levels deep; it must NOT count as a session.
        proj = os.path.join(self.home, "projects", "-Users-me-works-claude-skills")
        _jsonl(os.path.join(proj, self._fg, "subagents", "agent-deadbeef.jsonl"), [
            {"type": "assistant", "cwd": "/x", "message": {"role": "assistant",
             "content": [{"type": "text", "text": "child work"}]}}])
        snap = sd.build_snapshot(self.home, window_min=120)
        shorts = [s["short"] for s in snap["sessions"]]
        self.assertNotIn("agent-de", shorts)
        self.assertEqual(snap["counts"]["total"], 7)  # unchanged

    def test_command_shell_stripped_and_drift_flagged(self):  # P0-3
        snap = sd.build_snapshot(self.home, window_min=120)
        fg = self._by_short(snap, "aaaaaaaa")
        self.assertEqual(fg["opening"], "build the session radar dashboard")
        self.assertNotIn("<command", fg["opening"])
        self.assertTrue(fg["drift"])

    def test_current_repo_worktree_branch_from_tail(self):  # P0-2
        snap = sd.build_snapshot(self.home, window_min=120)
        fg = self._by_short(snap, "aaaaaaaa")
        # current action is in aifriends on fix/provider (NOT the worktree it opened in)
        self.assertEqual(fg["repo"], "aifriends")
        self.assertEqual(fg["branch"], "fix/provider")

    def test_bg_active_but_stale_is_working_not_dormant(self):  # P0-1 + P0-4
        snap = sd.build_snapshot(self.home, window_min=120)
        bg = self._by_short(snap, "32ea05ca")
        self.assertTrue(bg["is_bg"])
        self.assertEqual(bg["liveness"], "working")
        self.assertGreaterEqual(snap["counts"]["bg"], 1)

    def test_errored_tail_classified_errored(self):  # P0-1
        snap = sd.build_snapshot(self.home, window_min=120)
        err = self._by_short(snap, "bbbbbbbb")
        self.assertEqual(err["liveness"], "errored")
        self.assertTrue(err["errored"])
        self.assertGreaterEqual(snap["counts"]["errored"], 1)

    def test_window_filter_keeps_alive_bg_despite_stale_transcript(self):
        # The BG job's transcript mtime is 1h old; a 30-min window would drop a
        # plain fg session that old, but the bg job survives because its state is
        # alive. The fresh fg sessions remain present.
        snap = sd.build_snapshot(self.home, window_min=30)
        shorts = {s["short"] for s in snap["sessions"]}
        self.assertIn("32ea05ca", shorts)   # bg alive -> always surfaced despite 1h-old transcript
        self.assertIn("aaaaaaaa", shorts)    # fresh fg
        self.assertEqual(next(s for s in snap["sessions"] if s["short"] == "32ea05ca")["liveness"],
                         "working")

    def test_malformed_jsonl_does_not_crash(self):
        proj = os.path.join(self.home, "projects", "-Users-me-works-claude-skills")
        with open(os.path.join(proj, "cccccccc-9999.jsonl"), "w") as f:
            f.write("{not json\n")
        snap = sd.build_snapshot(self.home, window_min=120)  # must not raise
        self.assertIsInstance(snap["sessions"], list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v -k BuildSnapshot`
Expected: FAIL — `build_snapshot` not defined.

- [ ] **Step 3: Write minimal implementation** — append:

```python
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
    now_text = activity or (f"[bg:{bg_state}]" if bg_state else "—")
    return {
        "session_id": sid,
        "short": sid[:8],
        "age_s": round(age, 1),
        "liveness": live,
        "liveness_label": LIVENESS_LABELS.get(live, live),
        "is_bg": is_bg,
        "bg_state": bg_state,
        "repo": repo_of(cwd),
        "cwd": cwd,
        "worktree": worktree_of(cwd),
        "branch": branch or "—",
        "opening": opening or "—",
        "now": now_text,
        "errored": errored,
        "drift": compute_drift(opening, activity),
    }


def build_snapshot(home, window_min=WINDOW_MIN_DEFAULT, now=None):
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
    counts = {
        "total": len(rows),
        "bg": sum(1 for r in rows if r["is_bg"]),
        "errored": sum(1 for r in rows if r["errored"]),
        "drift": sum(1 for r in rows if r["drift"]),
    }
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "claude_home": home,
        "window_min": window_min,
        "counts": counts,
        "sessions": rows,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v`
Expected: PASS (all classes).

- [ ] **Step 5: Commit**

```bash
git add skills/session-radar/session_dashboard.py tests/session_radar/test_session_dashboard.py
git commit -m "feat(session-radar): build_snapshot aggregating fg + bg sessions with all P0 dims"
```

---

## Task 7: HTTP server + port polling + `render_html` + `render_table` + `main` (P0-5)

**Files:**
- Modify: `skills/session-radar/session_dashboard.py`
- Test: `tests/session_radar/test_session_dashboard.py`

- [ ] **Step 1: Write the failing test** — append:

```python
class RenderAndCliTest(unittest.TestCase):
    def test_render_html_is_selfcontained_and_fetches_api(self):
        html = sd.render_html()
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("/api/state", html)
        self.assertIn("Session Radar", html)

    def test_render_table_has_header_and_rows(self):
        snap = {"generated_at": "t", "counts": {"total": 1, "bg": 1, "errored": 0, "drift": 1},
                "sessions": [{"liveness_label": "🟢 working", "is_bg": True, "repo": "aifriends",
                              "branch": "main", "opening": "review F4", "now": "Read(sdk.py)",
                              "drift": True}]}
        out = sd.render_table(snap)
        self.assertIn("Session Radar", out)
        self.assertIn("aifriends", out)
        self.assertIn("DRIFT", out)

    def test_main_json_prints_snapshot(self):
        import io, contextlib
        home = tempfile.mkdtemp()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sd.main(["--claude-home", home, "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertIn("sessions", data)

    def test_main_once_prints_table(self):
        import io, contextlib
        home = tempfile.mkdtemp()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sd.main(["--claude-home", home, "--once"])
        self.assertEqual(rc, 0)
        self.assertIn("Session Radar", buf.getvalue())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v -k RenderAndCli`
Expected: FAIL — `render_html` / `render_table` / `main` not defined.

- [ ] **Step 3: Write minimal implementation** — append the HTML constant, server, and CLI:

```python
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
</style></head><body>
<header><h1>📡 Session Radar</h1>
<span class="sub" id="meta">loading…</span></header>
<main><table><thead><tr>
<th>Live</th><th>BG</th><th>Repo / Worktree</th><th>Branch</th>
<th>Opening → Now</th><th>Drift</th></tr></thead>
<tbody id="rows"></tbody></table></main>
<script>
function esc(s){return (s==null?"":""+s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function row(s){
  var wt = s.worktree ? '<div class="br">⟨wt:'+esc(s.worktree)+'⟩</div>' : '';
  var drift = s.drift ? '<span class="drift">⤳ DRIFT</span>' : '';
  return '<tr class="'+(s.errored?'err':'')+'">'
    +'<td class="live">'+esc(s.liveness_label)+'</td>'
    +'<td class="bg">'+(s.is_bg?'🤖':'')+'</td>'
    +'<td><span class="repo">'+esc(s.repo)+'</span>'+wt+'</td>'
    +'<td class="br">'+esc(s.branch)+'</td>'
    +'<td><span class="clip" title="'+esc(s.opening)+'">'+esc(s.opening)+'</span>'
      +'<span class="flow"> → </span>'
      +'<span class="now clip" title="'+esc(s.now)+'">'+esc(s.now)+'</span></td>'
    +'<td>'+drift+'</td></tr>';
}
function load(){
  fetch("/api/state").then(r=>r.json()).then(d=>{
    var c=d.counts||{};
    document.getElementById("meta").textContent=
      (c.total||0)+" sessions · "+(c.bg||0)+" bg · "+(c.errored||0)+" errored · "
      +(c.drift||0)+" drifted · "+esc(d.generated_at);
    document.getElementById("rows").innerHTML=(d.sessions||[]).map(row).join("");
  }).catch(e=>{document.getElementById("meta").textContent="error: "+e;});
}
load(); setInterval(load, 5000);
</script></body></html>"""


def render_html():
    return HTML


def render_table(snap):
    c = snap.get("counts", {})
    out = [f"📡 Session Radar — {c.get('total',0)} sessions "
           f"({c.get('bg',0)} bg, {c.get('errored',0)} errored, {c.get('drift',0)} drifted)"
           f"  @ {snap.get('generated_at','')}", "-" * 104,
           f"{'LIVE':<11} {'BG':<3} {'REPO/WT':<26} {'BRANCH':<22} OPENING → NOW"]
    for r in snap.get("sessions", []):
        bg = "🤖" if r.get("is_bg") else "  "
        repo = (r.get("repo") or "—")[:25]
        br = (r.get("branch") or "—")[:21]
        drift = "  ⤳DRIFT" if r.get("drift") else ""
        out.append(f"{r.get('liveness_label',''):<11} {bg:<3} {repo:<26} {br:<22} "
                   f"{(r.get('opening') or '—')[:32]} → {(r.get('now') or '—')[:32]}{drift}")
    return "\n".join(out)


class _Handler(BaseHTTPRequestHandler):
    home = os.path.expanduser("~/.claude")
    window_min = WINDOW_MIN_DEFAULT

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
            snap = build_snapshot(self.home, self.window_min)
            return self._send(200, json.dumps(snap, ensure_ascii=False),
                              "application/json; charset=utf-8")
        if self.path == "/" or self.path.startswith("/index"):
            return self._send(200, render_html(), "text/html; charset=utf-8")
        return self._send(404, "not found", "text/plain; charset=utf-8")


def serve(home, port=DEFAULT_PORT, window_min=WINDOW_MIN_DEFAULT, max_tries=20):
    """Bind 127.0.0.1:port, polling upward until a free port is found."""
    _Handler.home = os.path.expanduser(home)
    _Handler.window_min = window_min
    last_err = None
    for i in range(max_tries):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port + i), _Handler)
        except OSError as e:
            last_err = e
            continue
        actual = port + i
        print(f"📡 session-radar → http://127.0.0.1:{actual}  (claude_home={_Handler.home})")
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
    args = ap.parse_args(argv)
    if args.json:
        print(json.dumps(build_snapshot(args.claude_home, args.window_min), ensure_ascii=False, indent=2))
        return 0
    if args.once:
        print(render_table(build_snapshot(args.claude_home, args.window_min)))
        return 0
    serve(args.claude_home, args.port, args.window_min)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/session_radar/test_session_dashboard.py -v`
Expected: PASS (entire suite).

- [ ] **Step 5: Commit**

```bash
git add skills/session-radar/session_dashboard.py tests/session_radar/test_session_dashboard.py
git commit -m "feat(session-radar): http server with port polling, HTML + --once table + CLI"
```

---

## Task 8: E2E git/HTTP runner + project E2E config (the coverage contract, live)

**Files:**
- Create: `tests/session_radar/session_radar_e2e_runner.py`
- Create: `.claude/fastship.project.json`
- Modify: `.gitignore`

- [ ] **Step 1: Create the project E2E config** — `.claude/fastship.project.json`:

```json
{
  "e2e": {
    "result_path": "tmp/session_radar_e2e_result.json",
    "min_turns": 10,
    "setup_commands": [],
    "runner_command": "python3 tests/session_radar/session_radar_e2e_runner.py -o tmp/session_radar_e2e_result.json",
    "gate_command": "python3 skills/fastship/e2e/e2e_gate.py --result tmp/session_radar_e2e_result.json --min-turns 10",
    "notes": [
      "Pure-Python stdlib skill: no HTTP service of its own; the runner boots session_dashboard.py against a fake ~/.claude home and asserts business results.",
      "result_path is repo-local (resolved to <repo_root>/tmp via _absolute_project_path) to avoid shared /tmp collisions with parallel jobs."
    ]
  }
}
```

- [ ] **Step 2: Add the result file to .gitignore**

Append to `.gitignore` (create if absent):

```
# session-radar E2E artifacts (generated)
/tmp/
tmp/
```

- [ ] **Step 3: Write the E2E runner** — `tests/session_radar/session_radar_e2e_runner.py`:

```python
#!/usr/bin/env python3
"""Session Radar E2E runner (pure stdlib, no external service).

Builds a fake ~/.claude home whose fixtures deterministically trigger every P0
AC, then (a) exercises build_snapshot via the module directly and (b) boots the
real HTTP server as a subprocess and hits /healthz, /api/state, / with urllib.
Each turn asserts a business-observable result. Emits nested scenarios[].rounds[]
.turns (for fastship validate_e2e_report) plus flat turns/passed (for e2e_gate).
"""
import argparse, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = os.path.join(ROOT, "skills", "session-radar", "session_dashboard.py")
sys.path.insert(0, os.path.join(ROOT, "skills", "session-radar"))
import importlib.util
spec = importlib.util.spec_from_file_location("session_dashboard", MODULE)
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _jsonl(path, objs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def _build_home():
    home = tempfile.mkdtemp(prefix="session_radar_e2e_")
    proj = os.path.join(home, "projects", "-Users-me-works-claude-skills")
    # FG /fastship session, drifted (opened to build radar, now opening a PR elsewhere)
    fg = "aaaaaaaa-1111-2222-3333-444444444444"
    _jsonl(os.path.join(proj, fg + ".jsonl"), [
        {"type": "user", "cwd": "/Users/me/works/claude-skills/.claude/worktrees/session-radar",
         "gitBranch": "feat/session-radar-dashboard",
         "message": {"role": "user", "content":
             "<command-message>fastship</command-message>"
             "<command-args>build the session radar dashboard</command-args>"}},
        {"type": "assistant", "cwd": "/Users/me/works/aifriends", "gitBranch": "fix/provider",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Bash", "input": {"command": "git push origin pr-branch"}}]}},
    ])
    # BG job alive but transcript 1h stale -> must read as 'working'
    bg = "32ea05ca-71ef-40de-8b31-b1c929992902"
    bgp = os.path.join(proj, bg + ".jsonl")
    _jsonl(bgp, [
        {"type": "user", "cwd": "/Users/me/works/aifriends", "gitBranch": "main",
         "message": {"role": "user", "content": "review F4 SDK alignment"}},
        {"type": "assistant", "cwd": "/Users/me/works/aifriends", "gitBranch": "main",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Read", "input": {"file_path": "sdk.py"}}]}},
    ])
    old = time.time() - 3600
    os.utime(bgp, (old, old))
    _write(os.path.join(home, "jobs", "32ea05ca", "state.json"),
           {"state": "active", "intent": "review F4 SDK alignment",
            "cwd": "/Users/me/works/aifriends", "updatedAt": "2026-06-03T09:00:00Z",
            "linkScanPath": bgp})
    # Errored FG session
    err = "bbbbbbbb-5555-6666-7777-888888888888"
    _jsonl(os.path.join(proj, err + ".jsonl"), [
        {"type": "user", "cwd": "/repo", "gitBranch": "main",
         "message": {"role": "user", "content": "do the thing"}},
        {"type": "assistant", "cwd": "/repo", "gitBranch": "main",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "API Error: 529 overloaded_error"}]}},
    ])
    # FG session whose CURRENT action is inside a .claude/worktrees/<name> cwd
    wt = "dddddddd-aaaa-bbbb-cccc-dddddddddddd"
    _jsonl(os.path.join(proj, wt + ".jsonl"), [
        {"type": "user", "cwd": "/Users/me/works/claude-skills", "gitBranch": "main",
         "message": {"role": "user", "content": "ship the radar"}},
        {"type": "assistant",
         "cwd": "/Users/me/works/claude-skills/.claude/worktrees/session-radar",
         "gitBranch": "feat/session-radar-dashboard",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Edit", "input": {"file_path": "session_dashboard.py"}}]}},
    ])
    # DONE background job with NO live transcript in projects/ -> must still surface
    done = "cafe1234"
    _write(os.path.join(home, "jobs", done, "state.json"),
           {"state": "done", "intent": "earlier finished job",
            "cwd": "/Users/me/works/other", "updatedAt": "2026-06-01T00:00:00Z"})
    # BLOCKED background job (alive, awaiting input)
    blk = "beef5678"
    _write(os.path.join(home, "jobs", blk, "state.json"),
           {"state": "blocked", "intent": "waiting on permission", "cwd": "/Users/me/works/other"})
    # STATELESS background job: dir, no state.json -> liveness 'unknown', never 'active'
    nost = "0d0d0d0d"
    os.makedirs(os.path.join(home, "jobs", nost))
    # OLD dormant FOREGROUND session (30 days) — hidden by the default window,
    # but in scope when --window-min 0 (proves the window is a lens, not a cap)
    oldfg = "eeeeeeee-0000-1111-2222-333333333333"
    op = os.path.join(proj, oldfg + ".jsonl")
    _jsonl(op, [
        {"type": "user", "cwd": "/old/repo", "gitBranch": "main",
         "message": {"role": "user", "content": "ancient task"}},
        {"type": "assistant", "cwd": "/old/repo", "gitBranch": "main",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "done long ago"}]}},
    ])
    old2 = time.time() - 30 * 24 * 3600
    os.utime(op, (old2, old2))
    return home, {"fg": fg[:8], "bg": bg[:8], "err": err[:8], "wt": wt[:8],
                  "done": done[:8], "blk": blk[:8], "nost": nost[:8], "oldfg": oldfg[:8]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="tmp/session_radar_e2e_result.json")
    args = ap.parse_args()

    home, ids = _build_home()
    turns = []

    def turn(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"exception: {e}"
        turns.append({"turn": len(turns) + 1, "action": name,
                      "response": detail, "status": "pass" if ok else "fail",
                      "error": "" if ok else detail, "expect_ok": bool(ok)})

    # ---- in-process snapshot assertions (P0-1..P0-4) ----
    snap = sd.build_snapshot(home, window_min=120)
    byshort = {s["short"]: s for s in snap["sessions"]}

    turn("snapshot has 7 sessions (2 fg + 1 worktree fg + 4 bg)",
         lambda: (snap["counts"]["total"] == 7, f"total={snap['counts']['total']}"))
    turn("P0-4 scope: fg + all 4 bg kinds present; counts.bg == 4",
         lambda: (all(ids[k] in byshort for k in ("fg", "bg", "done", "blk", "nost"))
                  and snap["counts"]["bg"] == 4,
                  f"bg={snap['counts']['bg']} shorts={sorted(byshort)}"))
    turn("P0-4 done bg job (no transcript) surfaces with liveness 'done' + intent opening",
         lambda: (byshort[ids["done"]]["liveness"] == "done"
                  and byshort[ids["done"]]["is_bg"] is True
                  and byshort[ids["done"]]["opening"] == "earlier finished job",
                  f"done_row={byshort[ids['done']]['liveness']}/{byshort[ids['done']]['opening']!r}"))
    turn("P0-4 blocked bg job surfaces with liveness 'blocked'",
         lambda: (byshort[ids["blk"]]["liveness"] == "blocked" and byshort[ids["blk"]]["is_bg"] is True,
                  f"blk_row={byshort[ids['blk']]['liveness']}"))
    turn("P0-1 stateless bg job (no state.json) is 'unknown', never 'active' off fabricated age",
         lambda: (byshort[ids["nost"]]["liveness"] == "unknown" and byshort[ids["nost"]]["is_bg"] is True,
                  f"nost_row={byshort[ids['nost']]['liveness']}"))
    turn("P0-4 window is a recency LENS not a scope cap: old fg hidden@120, in scope@0",
         lambda: (ids["oldfg"] not in byshort
                  and ids["oldfg"] in {s["short"] for s in
                                       sd.build_snapshot(home, window_min=0)["sessions"]},
                  "window opt-out surfaces all foreground sessions"))
    turn("P0-3 command-shell stripped: opening is real human intent",
         lambda: (byshort[ids["fg"]]["opening"] == "build the session radar dashboard"
                  and "<command" not in byshort[ids["fg"]]["opening"],
                  f"opening={byshort[ids['fg']]['opening']!r}"))
    turn("P0-3 drift flagged when current action diverges from opening",
         lambda: (byshort[ids["fg"]]["drift"] is True and snap["counts"]["drift"] >= 1,
                  f"drift={byshort[ids['fg']]['drift']}"))
    turn("P0-2 current repo/branch derived from transcript TAIL (not opening worktree)",
         lambda: (byshort[ids["fg"]]["repo"] == "aifriends"
                  and byshort[ids["fg"]]["branch"] == "fix/provider",
                  f"repo={byshort[ids['fg']]['repo']} br={byshort[ids['fg']]['branch']}"))
    turn("P0-2 worktree session ROW exposes worktree+repo+branch from current cwd",
         lambda: (byshort[ids["wt"]]["worktree"] == "session-radar"
                  and byshort[ids["wt"]]["repo"] == "claude-skills ⟨wt:session-radar⟩"
                  and byshort[ids["wt"]]["branch"] == "feat/session-radar-dashboard",
                  f"wt_row={byshort[ids['wt']]['repo']}/{byshort[ids['wt']]['worktree']}"))
    turn("P0-1 bg job alive-but-stale reads 'working' (state beats mtime)",
         lambda: (byshort[ids["bg"]]["liveness"] == "working" and byshort[ids["bg"]]["is_bg"] is True,
                  f"live={byshort[ids['bg']]['liveness']} age={byshort[ids['bg']]['age_s']}"))
    turn("P0-1 errored tail classified 'errored' (not reported as work)",
         lambda: (byshort[ids["err"]]["liveness"] == "errored"
                  and byshort[ids["err"]]["errored"] is True
                  and snap["counts"]["errored"] >= 1,
                  f"live={byshort[ids['err']]['liveness']}"))

    # ---- live HTTP server assertions (P0-5) ----
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, MODULE, "--claude-home", home, "--port", str(port), "--window-min", "120"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        up = False
        for _ in range(100):
            try:
                if _get(base + "/healthz")[0] == 200:
                    up = True; break
            except Exception:
                time.sleep(0.1)
        turn("P0-5 server healthz 200", lambda: (up, "up" if up else "server never came up"))

        st, body = _get(base + "/api/state")
        api = json.loads(body)
        apibyshort = {s["short"]: s for s in api["sessions"]}
        turn("P0-5 /api/state 200 + all 7 sessions over HTTP",
             lambda: (st == 200 and api["counts"]["total"] == 7, f"total={api['counts']['total']}"))
        turn("P0-5 /api/state preserves working+done+blocked+unknown+errored+worktree over HTTP",
             lambda: (
                 apibyshort[ids["bg"]]["liveness"] == "working"
                 and apibyshort[ids["done"]]["liveness"] == "done"
                 and apibyshort[ids["blk"]]["liveness"] == "blocked"
                 and apibyshort[ids["nost"]]["liveness"] == "unknown"
                 and apibyshort[ids["err"]]["liveness"] == "errored"
                 and apibyshort[ids["wt"]]["worktree"] == "session-radar",
                 "http snapshot coherent across all derived states"))
        sh, html = _get(base + "/")
        turn("P0-5 GET / serves HTML that fetches /api/state",
             lambda: (sh == 200 and "/api/state" in html and "<!DOCTYPE html>" in html
                      and "Session Radar" in html, "html ok"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)

    # ---- --once / --json CLI smoke over a real subprocess ----
    home2, _ = _build_home()
    try:
        once = subprocess.run([sys.executable, MODULE, "--claude-home", home2, "--once"],
                              capture_output=True, text=True, timeout=30)
        turn("P0-5 --once table shows real rows: repo + DRIFT marker + worktree",
             lambda: (once.returncode == 0 and "aifriends" in once.stdout
                      and "DRIFT" in once.stdout and "session-radar" in once.stdout,
                      f"once rows present"))
        jrun = subprocess.run([sys.executable, MODULE, "--claude-home", home2, "--json"],
                              capture_output=True, text=True, timeout=30)
        turn("P0-5 --json snapshot carries the derived fields (7 sessions, bg=4)",
             lambda: (jrun.returncode == 0
                      and json.loads(jrun.stdout)["counts"]["total"] == 7
                      and json.loads(jrun.stdout)["counts"]["bg"] == 4,
                      "json business fields present"))
    finally:
        shutil.rmtree(home2, ignore_errors=True)

    passed = all(t["expect_ok"] for t in turns)
    result = {
        "name": "session-radar-e2e",
        "status": "pass" if passed else "fail",
        "turns": turns,                    # flat (for e2e_gate.py smell checks)
        "passed": passed,
        "turn_count": len(turns),
        "scenarios": [{                    # nested (for fastship validate_e2e_report)
            "name": "session-radar-e2e",
            "description": "fake ~/.claude home -> build_snapshot + live HTTP server; all P0 ACs",
            "rounds": [{"turns": turns}],
        }],
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the runner and verify ≥10 turns all pass**

Run: `python3 tests/session_radar/session_radar_e2e_runner.py -o tmp/session_radar_e2e_result.json`
Expected: exit 0; stdout JSON with `"passed": true` and `turn_count` == 18. Then verify the nested turn count the way fastship does:

Run: `python3 -c "import json;d=json.load(open('tmp/session_radar_e2e_result.json'));print('nested_turns=',sum(len(r['turns']) for s in d['scenarios'] for r in s['rounds']))"`
Expected: `nested_turns= 18` (≥ 10).

- [ ] **Step 5: Run the fastship gate against the result**

Run: `python3 skills/fastship/e2e/e2e_gate.py --result tmp/session_radar_e2e_result.json --min-turns 10`
Expected: exit 0, prints `GATE PASSED`.

- [ ] **Step 6: Commit**

```bash
git add tests/session_radar/session_radar_e2e_runner.py .claude/fastship.project.json .gitignore
git commit -m "test(session-radar): pure-Python E2E runner covering all P0 ACs + project E2E config"
```

---

## Task 9: SKILL.md + launcher + run the whole suite

**Files:**
- Create: `skills/session-radar/SKILL.md`
- Create: `skills/session-radar/session-radar`
- Test: full unit suite

- [ ] **Step 1: Write the skill doc** — `skills/session-radar/SKILL.md`:

```markdown
---
name: session-radar
description: Stdlib-only dashboard for ALL local Claude sessions (foreground transcripts + background jobs). Shows liveness, the repo/worktree/branch each session is acting on right now, and opening-intent→current-action drift. Triggers: "session radar", "what are my sessions doing", "show running claude sessions".
---

# Session Radar

Scans `~/.claude/projects/*/*.jsonl` + `~/.claude/jobs/` and surfaces, per session:

- **Liveness** — active / idle / dormant / errored. Background jobs trust their own
  `state` (`working` / `blocked` / `done`) instead of transcript mtime, so a job that
  is alive but quiet between turns is not mislabelled dormant.
- **Current repo / worktree / branch** — derived from the *latest* transcript event,
  so it reflects where the session is acting now, not where it started.
- **opening → NOW drift** — the real human opening intent (command-shell stripped)
  beside the current action; flagged when they diverge.

## Usage

```bash
# Web dashboard (port polls upward from 7575 if busy), client-rendered, 5s refresh
python3 skills/session-radar/session_dashboard.py
# or via launcher
skills/session-radar/session-radar

# One-shot terminal table
python3 skills/session-radar/session_dashboard.py --once

# Raw snapshot JSON
python3 skills/session-radar/session_dashboard.py --json

# Options
#   --claude-home DIR   root of ~/.claude (default ~/.claude)
#   --port N            base port (default 7575)
#   --window-min N      recency lens for foreground sessions (default 120; 0 = show ALL; bg always shown)
```

Pure Python stdlib — no dependencies, no build step.
```

- [ ] **Step 2: Write the launcher** — `skills/session-radar/session-radar`:

```bash
#!/usr/bin/env bash
# Launch the session radar web dashboard.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/session_dashboard.py" "$@"
```

- [ ] **Step 3: Make the launcher executable**

Run: `chmod +x skills/session-radar/session-radar`
Expected: no output, exit 0.

- [ ] **Step 4: Run the FULL unit suite**

Run: `python3 -m pytest tests/session_radar/ -v`
Expected: PASS, all tests across all classes (≈ 30 tests), 0 failures, 0 errors.

- [ ] **Step 5: Smoke the launcher --once**

Run: `skills/session-radar/session-radar --once`
Expected: exit 0, prints a `📡 Session Radar — N sessions …` table against the real `~/.claude`.

- [ ] **Step 6: Commit**

```bash
git add skills/session-radar/SKILL.md skills/session-radar/session-radar
git commit -m "docs(session-radar): SKILL.md + launcher"
```

---

## Task 10: Remove the seed (it is not a deliverable)

**Files:**
- Delete: `_session_radar_seed/`

- [ ] **Step 1: Confirm the directory is the throwaway seed**

Run: `ls _session_radar_seed/`
Expected: `PROTOTYPE_session_radar.py  SEED.md` (the validation-phase scratch, explicitly "ship 前删掉" per SEED.md).

- [ ] **Step 2: Remove it**

Run: `git rm -r _session_radar_seed/`
Expected: both files staged for deletion.

- [ ] **Step 3: Re-run the unit suite to prove nothing depended on the seed**

Run: `python3 -m pytest tests/session_radar/ -q`
Expected: PASS (the module/tests/runner never import from `_session_radar_seed/`).

- [ ] **Step 4: Commit**

```bash
git commit -m "chore(session-radar): drop _session_radar_seed scratch (not a deliverable)"
```

---

## Phase-3 verification (explicit, ordered — for the fastship E2E gate)

These are exactly what `.claude/fastship.project.json` pins. Run every command from the repo root. Steps 3.1–3.4 + 3.3-report are individually load-bearing — none may be skipped.

- [ ] **Step 3.1 — Project tests.** Run: `python3 -m pytest tests/session_radar/ -v`. Expected: all pass; output contains `N passed` (hook sets `test_passed`).

- [ ] **Step 3.2 — E2E runner.** Run: `python3 tests/session_radar/session_radar_e2e_runner.py -o tmp/session_radar_e2e_result.json`. Expected: exit 0, `"passed": true`, 18 turns. The hook records `e2e_result_hash` = sha256 of the result file. Do **not** edit `tmp/session_radar_e2e_result.json` afterward.

- [ ] **Step 3.3 — Write the E2E report and bind it (do NOT skip).** This is a required, separate artifact: `validate_e2e_report` (`skills/fastship/orchestrator.py:813-865`) demands a report whose `report_path` was recorded by the current step AND whose text contains the literal `e2e_result_hash` from gate.json.
  1. Read the recorded hash: `python3 -c "import json;print(json.load(open('.git/.../gate.json'))['e2e_result_hash'])"` — or take it from `fastship status`. (The exact gate.json path is the session's `gate_state_path()`; `fastship status` prints it.)
  2. Write `docs/superpowers/reports/2026-06-03-session-radar-e2e.md` (≥ 200 bytes) summarizing the 18 turns and their P0 coverage, and include a line `e2e_result_hash: <the hash>` quoting it verbatim.
  3. Bind it: `"$(git rev-parse --show-toplevel)/.claude/tools/fastship" done --report docs/superpowers/reports/2026-06-03-session-radar-e2e.md`. The validator re-hashes the result file and re-reads the report; a mismatch or a missing hash line FAILs Step 3.3.

- [ ] **Step 3.4 — E2E gate.** Run: `python3 skills/fastship/e2e/e2e_gate.py --result tmp/session_radar_e2e_result.json --min-turns 10`. Expected: exit 0, prints `GATE PASSED` (hook sets `e2e_gate_passed`).

> In Claude Code hook mode these are auto-detected from the bash runs; in CLI mode each is driven via `ship_verify_gate.py post_bash` with the real `{command, stdout, exitCode}`. Either way the report (Step 3.3) is an explicit human/agent-authored artifact, not a by-product of the runner.

---

## Self-Review

**1. Spec coverage** — every SEED requirement maps to a task:
- liveness incl. bg + errored (hard problems #2,#3) → Tasks 3, 4; E2E P0-1.
- current repo/worktree/branch → Task 5; E2E P0-2.
- opening→NOW drift + command-shell strip (hard problem #1) → Tasks 1, 2, 5; E2E P0-3.
- scope = all sessions incl. bg → Task 6; E2E P0-4.
- reuse stdlib web shell + `--once` → Task 7; E2E P0-5.
- delete seed → Task 10. All three SEED "hard problems" are solved by dedicated unit-tested functions, not merely flagged.

**2. Placeholder scan** — no `TBD`/`later`/"add error handling"; every code step is complete and runnable. Error handling is concrete (`try/except OSError`, malformed-jsonl tolerance test).

**3. Type/name consistency** — function names used downstream match definitions: `strip_command_shell`, `extract_opening`, `extract_activity`, `is_error_text`, `bg_jobs`, `liveness`, `worktree_of`, `repo_of`, `compute_drift`, `build_snapshot`, `render_html`, `render_table`, `serve`, `main`. Row dict keys (`short`, `liveness`, `liveness_label`, `is_bg`, `bg_state`, `repo`, `worktree`, `branch`, `opening`, `now`, `errored`, `drift`) are produced in `_row_from_objs` (Task 6) and consumed identically in the HTML JS, `render_table` (Task 7), and the E2E runner (Task 8). Liveness states (`active/idle/dormant/errored/working/blocked/done/unknown`) are consistent across `liveness`, `LIVENESS_LABELS`, and all asserts. `_BG_ALIVE`/`_BG_WAIT`/`_BG_DONE` defined in Task 4 and reused by `liveness`. E2E result schema includes both nested `scenarios[].rounds[].turns` and flat `turns`/`passed` per KNOWLEDGE.md.

**4. Codex-review revisions (1.5c)** — applied after the independent codex review surfaced 4 [P1] + 2 [P2]:
- **Scope = ALL bg jobs** (was downgraded): `build_snapshot` now always surfaces every `~/.claude/jobs/` entry — alive, blocked, done, unknown, and stateless/transcript-less alike. The window filter trims only quiet foreground sessions. Covered by `test_p0_4_done_bg_job_without_transcript_still_surfaces`, `test_p0_4_blocked_bg_job_surfaces_as_blocked`, `test_p0_1_stateless_bg_job_is_unknown_not_active`, and E2E turns for done/blocked/stateless bg (counts.bg==4).
- **Stateless-bg liveness bug fixed** (2nd codex round): a bg job with no `state.json` had a fabricated `age=0.0` that leaked through to the mtime buckets and read `active`. `liveness` now classifies bg jobs by state ONLY (never mtime) and returns `unknown` for missing/unrecognized state — fully honoring hard problem #2. New liveness state `unknown` added to `LIVENESS_LABELS`.
- **P0-2 worktree as a business result**: added a worktree-active fixture session; `test_p0_2_worktree_row_exposes_worktree_and_repo` and the E2E "worktree session ROW" turn now assert a real snapshot row's `worktree`/`repo`/`branch`, not just a direct `worktree_of()` call.
- **Subagent exclusion documented + tested**: `build_snapshot` glob comment explains the 2-level depth; `test_subagent_transcripts_excluded` plants a 4-level subagent transcript and proves it is not counted.
- **Step 3.3 report is now an explicit ordered step** binding `e2e_result_hash`, not a footnote.
- **Robustness**: `liveness` uses `str(bg_state).lower()`; weak `--once`/`--json` E2E turns upgraded to assert real field values (repo, DRIFT marker, counts). Turn count 14 → 18.
- **CJK drift bug fixed** (3rd codex round): `compute_drift` tokenized `[a-z0-9]+` only, so a Chinese opening (the seed's own `做 …` is Chinese) produced zero tokens and drift never fired. `_TOKEN_RE` now also matches individual CJK ideographs. Covered by `test_cjk_opening_participates_in_drift` + the documented note that drift is a best-effort highlight (the side-by-side columns are the real signal).
- **Window scope made provably opt-out** (3rd codex round): the `--window-min` default (120) is a recency LENS for foreground sessions, not a scope cap — `--window-min 0` shows ALL foreground sessions, and background jobs are always shown regardless. Covered by `test_window_min_zero_shows_all_foreground_including_old` + E2E "window is a recency lens" turn, and documented in CLI help + SKILL.md.
