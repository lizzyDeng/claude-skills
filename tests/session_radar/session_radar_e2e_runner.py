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
