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
    # FG session sitting in aifriends but whose CURRENT command operates on another
    # repo (WT=/.../claude-skills-a4-s) -> grouped under aifriends, marked acting_on
    cross = "cccccccc-7777-8888-9999-aaaaaaaaaaaa"
    _jsonl(os.path.join(proj, cross + ".jsonl"), [
        {"type": "user", "cwd": "/Users/me/works/aifriends", "gitBranch": "main",
         "message": {"role": "user", "content": "给 claude-skills 做个 UI"}},
        {"type": "assistant", "cwd": "/Users/me/works/aifriends", "gitBranch": "main",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Bash",
              "input": {"command": "WT=/Users/me/works/claude-skills-a4-s git -C $WT add -A"}}]}},
    ])
    # SIGNAL-RICH foreground session: TaskCreate/TaskUpdate lifecycle + 6 edits +
    # 2 commits + a chatter tail. Drives the heuristic work-unit summarizer and the
    # injected-LLM headline turns (WORK-1..WORK-14). The CHATTER tail proves `now`
    # is the distilled unit of work, NOT the latest message.
    rich = "f1f1f1f1"
    rcwd, rbr = "/Users/me/works/claude-skills", "feat/work-unit-summary"
    rich_objs = [
        {"type": "user", "cwd": rcwd, "gitBranch": rbr,
         "message": {"role": "user", "content": "给雷达加工作单元摘要"}},
    ]
    for _ in range(6):
        rich_objs.append(
            {"type": "assistant", "cwd": rcwd, "gitBranch": rbr,
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Edit",
                  "input": {"file_path": rcwd + "/skills/session-radar/session_dashboard.py"}}]}})
    rich_objs += [
        {"type": "assistant", "cwd": rcwd, "gitBranch": rbr,
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "TaskCreate",
              "input": {"subject": "scaffold summarizer", "activeForm": "Scaffolding"}}]}},
        {"type": "assistant", "cwd": rcwd, "gitBranch": rbr,
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "TaskCreate",
              "input": {"subject": "wire row", "activeForm": "把 NOW 改成工作单元摘要"}}]}},
        {"type": "assistant", "cwd": rcwd, "gitBranch": rbr,
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "TaskUpdate",
              "input": {"taskId": "1", "status": "completed"}}]}},
        {"type": "assistant", "cwd": rcwd, "gitBranch": rbr,
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "TaskUpdate",
              "input": {"taskId": "2", "status": "in_progress"}}]}},
        {"type": "assistant", "cwd": rcwd, "gitBranch": rbr,
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Bash",
              "input": {"command": 'git commit -m "feat: distill session work unit"'}}]}},
        {"type": "assistant", "cwd": rcwd, "gitBranch": rbr,
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Bash",
              "input": {"command": 'git commit -m "feat: render work badge"'}}]}},
        {"type": "assistant", "cwd": rcwd, "gitBranch": rbr,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "好了，等你看效果。"}]}},
    ]
    _jsonl(os.path.join(proj, rich + ".jsonl"), rich_objs)
    # BARE foreground session: an opening but NO task / commit / edit signals —
    # exercises P0-4 graceful degradation (title falls back to the opening, never crash).
    bare = "b2b2b2b2"
    bcwd, bbr = "/Users/me/works/claude-skills", "main"
    _jsonl(os.path.join(proj, bare + ".jsonl"), [
        {"type": "user", "cwd": bcwd, "gitBranch": bbr,
         "message": {"role": "user", "content": "看一下这个问题怎么回事"}},
        {"type": "assistant", "cwd": bcwd, "gitBranch": bbr,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "我先看看上下文。"}]}},
    ])
    return home, {"fg": fg[:8], "bg": bg[:8], "err": err[:8], "wt": wt[:8],
                  "done": done[:8], "blk": blk[:8], "nost": nost[:8], "oldfg": oldfg[:8],
                  "cross": cross[:8], "rich": rich[:8], "bare": bare[:8]}


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

    turn("snapshot has 10 sessions (3 aifriends fg/bg + worktree fg + err + 2 other bg + stale + rich + bare)",
         lambda: (snap["counts"]["total"] == 10, f"total={snap['counts']['total']}"))
    turn("P0-4 scope: fg + all 4 bg kinds present; counts.bg == 4",
         lambda: (all(ids[k] in byshort for k in ("fg", "bg", "done", "blk", "nost"))
                  and snap["counts"]["bg"] == 4,
                  f"bg={snap['counts']['bg']} shorts={sorted(byshort)}"))
    turn("GROUP: sessions grouped by git project; aifriends group holds fg+bg+cross",
         lambda: (
             (lambda pm: "aifriends" in pm and "claude-skills" in pm
              and {ids["fg"], ids["bg"], ids["cross"]} <= {s["short"] for s in pm["aifriends"]["sessions"]}
              and ids["wt"] in {s["short"] for s in pm["claude-skills"]["sessions"]})(
                 {g["project"]: g for g in snap["projects"]}),
             f"projects={[g['project'] for g in snap['projects']]}"))
    turn("STALE: no-metadata bg collapsed into stale_unknown, excluded from groups",
         lambda: (snap["stale_unknown"] >= 1
                  and ids["nost"] not in {s["short"] for g in snap["projects"] for s in g["sessions"]},
                  f"stale_unknown={snap['stale_unknown']}"))
    turn("CROSS-REPO: session in aifriends operating on another repo is marked acting_on",
         lambda: (byshort[ids["cross"]]["project"] == "aifriends"
                  and byshort[ids["cross"]]["acting_on"] == "claude-skills-a4-s",
                  f"cross project={byshort[ids['cross']]['project']} acting_on={byshort[ids['cross']]['acting_on']}"))
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

    # ---- work-unit summary assertions (P0-1..P0-5), heuristic default (no LLM) ----
    rich = byshort[ids["rich"]]
    w = rich.get("work") or {}
    turn("WORK-1 type=feature + icon from branch prefix (feature/bugfix dimension)",
         lambda: (w.get("type") == "feature" and w.get("icon") == "🟢",
                  f"type={w.get('type')} icon={w.get('icon')}"))
    turn("WORK-2 doing = in-progress TaskUpdate item (agent's own current step)",
         lambda: ("摘要" in (w.get("doing") or ""), f"doing={w.get('doing')}"))
    turn("WORK-3 progress event-sourced from TaskCreate/TaskUpdate",
         lambda: (w.get("progress") == "1/2", f"progress={w.get('progress')}"))
    turn("WORK-4 detail distills focus file + commits across events",
         lambda: ("session_dashboard.py" in (w.get("detail") or "")
                  and "commit" in (w.get("detail") or ""), f"detail={w.get('detail')}"))
    turn("WORK-5 now synthesizes >=2 self-authored signals (branch title AND "
         "in-progress task), NOT the chatter tail",
         lambda: ("好了" not in rich.get("now", "")
                  and "work unit summary" in rich.get("now", "")      # signal 1: branch-derived title
                  and "摘要" in rich.get("now", ""),                   # signal 2: in-progress task
                  f"now={rich.get('now')}"))
    turn("WORK-6 default snapshot uses heuristic (no LLM dependency in E2E)",
         lambda: (w.get("source") == "heuristic", f"source={w.get('source')}"))
    bare = byshort[ids["bare"]]
    bw = bare.get("work") or {}
    turn("WORK-7 P0-4 graceful degradation: no task/commit/edit signal → title "
         "falls back to opening, no crash",
         lambda: (bw.get("source") == "heuristic"
                  and "看一下这个问题" in (bw.get("title") or "") and bw.get("doing") == "",
                  f"bare title={bw.get('title')} doing={bw.get('doing')}"))

    # ---- injected-stub-LLM work-unit assertions (deterministic LLM plumbing) ----
    sd._LLM_CACHE.clear(); sd._LLM_PENDING.clear()
    # Capture the heuristic drift BEFORE the LLM pass, to prove drift is LLM-independent.
    heur_rich = byshort[ids["rich"]]
    heur_drift = heur_rich["drift"]
    llm_snap = sd.build_snapshot(home, window_min=0,
                                 use_llm=True, llm=lambda p: "注入摘要：提炼工作单元",
                                 llm_block=True)
    lb = {s["short"]: s for s in llm_snap["sessions"]}
    rich_llm = lb[ids["rich"]]
    turn("WORK-8 injected LLM replaces the headline + marks source=llm",
         lambda: (rich_llm["now"] == "注入摘要：提炼工作单元"
                  and (rich_llm.get("work") or {}).get("source") == "llm",
                  f"now={rich_llm.get('now')} src={(rich_llm.get('work') or {}).get('source')}"))
    turn("WORK-9 non-stale background rows ARE distilled by the LLM too "
         "(a verbatim intent echo has no value); only info-less stale jobs stay heuristic",
         lambda: (
             any((lb[k].get("work") or {}).get("source") == "llm"
                 and lb[k].get("now") == "注入摘要：提炼工作单元"
                 for k in lb if lb[k]["is_bg"] and not lb[k].get("is_stale"))
             and all((lb[k].get("work") or {}).get("source") == "heuristic"
                     for k in lb if lb[k]["is_bg"] and lb[k].get("is_stale")),
             "non-stale bg distilled by LLM; stale bg stays heuristic"))
    turn("WORK-10 P0-5 drift is LLM-INDEPENDENT (fed heuristic title+doing)",
         lambda: (rich_llm["drift"] == heur_drift, f"llm_drift={rich_llm['drift']} heur={heur_drift}"))

    # P0-5 non-blocking: a SLOW LLM must NOT block /api/state — the first
    # non-blocking call returns the heuristic immediately and schedules the work.
    sd._LLM_CACHE.clear(); sd._LLM_PENDING.clear()
    def _slow_llm(prompt):
        raise AssertionError("non-blocking path must NOT call the LLM inline")
    nb_snap = sd.build_snapshot(home, window_min=0,
                                use_llm=True, llm=_slow_llm, llm_block=False)
    nb_rich = next(s for s in nb_snap["sessions"] if s["short"] == ids["rich"])
    turn("WORK-11 P0-5 non-blocking /api/state returns heuristic immediately "
         "(slow LLM scheduled, never inlined)",
         lambda: ((nb_rich.get("work") or {}).get("source") == "heuristic",
                  f"non-blocking src={(nb_rich.get('work') or {}).get('source')}"))

    # P0-3 LLM unavailable/error → fall back to the deterministic heuristic headline.
    sd._LLM_CACHE.clear(); sd._LLM_PENDING.clear()
    fb_snap = sd.build_snapshot(home, window_min=0,
                                use_llm=True, llm=lambda p: None, llm_block=True)
    fb_rich = next(s for s in fb_snap["sessions"] if s["short"] == ids["rich"])
    turn("WORK-12 P0-3 LLM error/unavailable → heuristic fallback (source=heuristic, "
         "now == heuristic summary)",
         lambda: ((fb_rich.get("work") or {}).get("source") == "heuristic"
                  and "work unit summary" in fb_rich.get("now", ""),
                  f"fallback src={(fb_rich.get('work') or {}).get('source')} now={fb_rich.get('now')}"))

    # P0-5 --once terminal table renders the work-unit summary + progress.
    once_out = sd.render_table(llm_snap)
    turn("WORK-13 P0-5 --once table renders the work-unit summary + progress",
         lambda: ("注入摘要：提炼工作单元" in once_out and "1/2" in once_out,
                  "once table carries summary+progress"))

    # ---- live HTTP server assertions (P0-5) ----
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, MODULE, "--claude-home", home, "--port", str(port),
         "--window-min", "120", "--no-llm"],
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
        turn("P0-5 /api/state 200 + all 10 sessions + project groups over HTTP",
             lambda: (st == 200 and api["counts"]["total"] == 10
                      and api["counts"]["projects"] == len(api["projects"])
                      and api["stale_unknown"] >= 1, f"total={api['counts']['total']}"))
        turn("P0-5 /api/state preserves working+done+blocked+unknown+errored+worktree over HTTP",
             lambda: (
                 apibyshort[ids["bg"]]["liveness"] == "working"
                 and apibyshort[ids["done"]]["liveness"] == "done"
                 and apibyshort[ids["blk"]]["liveness"] == "blocked"
                 and apibyshort[ids["nost"]]["liveness"] == "unknown"
                 and apibyshort[ids["err"]]["liveness"] == "errored"
                 and apibyshort[ids["wt"]]["worktree"] == "session-radar",
                 "http snapshot coherent across all derived states"))
        rich_http = apibyshort.get(ids["rich"])
        turn("WORK-14 work-unit summary preserved over HTTP /api/state",
             lambda: (bool(rich_http) and (rich_http.get("work") or {}).get("type") == "feature"
                      and "摘要" in ((rich_http.get("work") or {}).get("doing") or ""),
                      "http work summary coherent"))
        sh, html = _get(base + "/")
        turn("P0-5 GET / serves grouped HTML (project header + stale line wired)",
             lambda: (sh == 200 and "/api/state" in html and "<!DOCTYPE html>" in html
                      and "Session Radar" in html and "grpHead" in html
                      and "stale_unknown" in html, "html ok"))
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
        once = subprocess.run([sys.executable, MODULE, "--claude-home", home2, "--once", "--no-llm"],
                              capture_output=True, text=True, timeout=30)
        turn("P0-5 --once table groups by project (📁 header) + DRIFT + worktree + stale line",
             lambda: (once.returncode == 0 and "📁 aifriends" in once.stdout
                      and "DRIFT" in once.stdout and "session-radar" in once.stdout
                      and "无元数据的旧后台任务" in once.stdout,
                      f"once grouped rows present"))
        jrun = subprocess.run([sys.executable, MODULE, "--claude-home", home2, "--json", "--no-llm"],
                              capture_output=True, text=True, timeout=30)
        turn("P0-5 --json snapshot carries the derived fields (10 sessions, bg=4, projects)",
             lambda: (jrun.returncode == 0
                      and json.loads(jrun.stdout)["counts"]["total"] == 10
                      and json.loads(jrun.stdout)["counts"]["bg"] == 4
                      and len(json.loads(jrun.stdout)["projects"]) >= 1,
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
