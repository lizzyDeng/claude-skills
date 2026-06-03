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
    project = project_of(cwd)
    # A "stale" row is a background job with no usable metadata at all (no cwd,
    # no intent) — a cleaned-up / aborted ~/.claude/jobs entry. Collapsed in the
    # UI instead of shown as an empty row.
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
        "now": now_text,
        "errored": errored,
        "drift": compute_drift(opening, activity),
        "is_stale": is_stale,
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
    # Group the metadata-bearing rows by their MAIN git repo; collapse the
    # no-metadata stale jobs into a single count.
    stale = [r for r in rows if r["is_stale"]]
    visible = [r for r in rows if not r["is_stale"]]
    by_project = {}
    for r in visible:
        by_project.setdefault(r["project"], []).append(r)
    projects = [{"project": p,
                 "sessions": sorted(sess, key=lambda r: r["age_s"]),
                 "count": len(sess)}
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
</style></head><body>
<header><h1>📡 Session Radar</h1>
<span class="sub" id="meta">loading…</span></header>
<main><table><thead><tr>
<th>Live</th><th>BG</th><th>Worktree</th><th>Branch</th>
<th>Opening → Now</th><th>Drift</th></tr></thead>
<tbody id="rows"></tbody></table></main>
<script>
function esc(s){return (s==null?"":""+s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function grpHead(g){
  return '<tr class="grp"><td colspan="6">'
    +'<span class="pname">📁 '+esc(g.project)+'</span>'
    +'<span class="pcount">'+g.count+' session'+(g.count>1?'s':'')+'</span></td></tr>';
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
      +'<span class="flow"> → </span>'
      +'<span class="now clip" title="'+esc(s.now)+'">'+esc(s.now)+'</span>'+act+'</td>'
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
load(); setInterval(load, 5000);
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
            out.append(f"  {r.get('liveness_label',''):<11} {bg:<3} {wt:<22} {br:<20} "
                       f"{(r.get('opening') or '—')[:30]} → {(r.get('now') or '—')[:30]}{act}{drift}")
    stale = snap.get("stale_unknown", 0)
    if stale:
        out.append("")
        out.append(f"+ {stale} 无元数据的旧后台任务（jobs/ 里无 state.json，已折叠）")
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
