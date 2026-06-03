#!/usr/bin/env python3
"""E2E: build a fixture repo with known forge+fastship state, boot the real
forge dashboard server against it, and assert real business structure
(objectives, obj-4 split rollup/TODO, feature<->session linkage, progress math).
Deterministic - no dependency on the live repo. Emits e2e_runner schema."""
import argparse, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.request

# tests/forge/forge_dashboard_e2e.py -> repo root -> skills/forge (where the tool lives in source)
TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skills", "forge")

SPLIT = ["persona-pg-store", "boyfriend-chat-route", "persona-tool-executor",
         "persona-image-generator", "persona-image-processor",
         "persona-cron-loops", "persona-e2e-parity"]


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _build_fixture():
    """A git repo with: 4 objectives; obj-4 = 7 draft split features (the
    'big feature split into many roadmaps' case); a concluded feature with
    metric+harvest; and one in_progress feature linked to a fastship session."""
    tmp = tempfile.mkdtemp(prefix="forge_e2e_")
    _genv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True)
    subprocess.run(["git", "-C", tmp, "commit", "-q", "--allow-empty", "-m", "init"], check=True, env=_genv)
    features = [
        {"slug": "telegram-binding", "name": "Telegram bind", "objective_id": "obj-1", "status": "in_progress"},
        {"slug": "admin-stats", "name": "Admin Stats", "objective_id": "obj-1", "status": "concluded"},
        {"slug": "themed-bubbles", "name": "Themed Bubbles", "objective_id": "obj-2", "status": "shipped"},
        {"slug": "memory-v3", "name": "Memory v3", "objective_id": "obj-3", "status": "planned"},
    ] + [{"slug": s, "name": s, "objective_id": "obj-4", "status": "draft"} for s in SPLIT]
    _write(os.path.join(tmp, "project-roadmap", "roadmap.json"), {
        "north_star": "be the AI companion users use 30 min/day",
        "objectives": [
            {"id": "obj-1", "name": "stickiness", "target_metric": "session >= 30min"},
            {"id": "obj-2", "name": "emotional realism", "target_metric": "satisfaction >= 4.2/5"},
            {"id": "obj-3", "name": "memory coherence", "target_metric": "recall >= 80%"},
            {"id": "obj-4", "name": "Persona Engine SDK", "target_metric": "SDK parity, E2E all pass"},
        ],
        "features": features,
    })
    _write(os.path.join(tmp, "project-roadmap", "features", "admin-stats", "metric.json"),
           {"baseline": 0, "target": 1, "metric_name": "stats page shipped"})
    _write(os.path.join(tmp, "project-roadmap", "features", "admin-stats", "harvest.json"),
           {"actual": 1, "baseline": 0, "target": 1, "verdict": "achieved", "next_action": "done"})
    sess = os.path.join(tmp, ".git", "fastship", "sessions", "telegram-binding")
    _write(os.path.join(sess, "orchestrator.json"), {
        "session_id": "telegram-binding", "requirement": "Telegram bind",
        "current_step": "2.0", "phase": 2, "branch": "feat/telegram-binding",
        "completed_steps": ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.5c", "1.6"],
        "skipped_steps": ["1.3d"], "started_at": "2026-05-29T19:24:31",
    })
    _write(os.path.join(sess, "gate.json"),
           {"forge_feature": "telegram-binding", "test_passed": True,
            "e2e_executed": False, "e2e_gate_passed": False, "request_type": "feature"})
    # stale DUPLICATE of linked telegram-binding (older) -> must NOT leak into Other
    dup = os.path.join(tmp, ".git", "fastship", "sessions", "telegram-binding-old")
    _write(os.path.join(dup, "orchestrator.json"),
           {"session_id": "telegram-binding-old", "current_step": "1.4", "completed_steps": [], "skipped_steps": []})
    _write(os.path.join(dup, "gate.json"), {})
    for _fn in ("orchestrator.json", "gate.json"):
        os.utime(os.path.join(dup, _fn), (1_000_000, 1_000_000))
    # REAL worktree whose basename == an obj-4 slug, NO session (exercises porcelain fallback)
    subprocess.run(["git", "-C", tmp, "worktree", "add", "-q", "-b", "feat/img-gen",
                    os.path.join(tmp, "wt", "persona-image-generator")], check=True)
    # REAL worktree "wt-bf" on LIVE branch feat/bf-live; its session records a STALE branch
    subprocess.run(["git", "-C", tmp, "worktree", "add", "-q", "-b", "feat/bf-live",
                    os.path.join(tmp, "wt", "wt-bf")], check=True)
    common = os.path.join(tmp, ".git")
    bf = os.path.join(common, "worktrees", "wt-bf", "fastship", "sessions", "boyfriend-chat-route")
    _write(os.path.join(bf, "orchestrator.json"),
           {"session_id": "boyfriend-chat-route", "requirement": "bf chat", "current_step": "2.0",
            "branch": "feat/bf-STALE", "completed_steps": ["1.0", "1.1"], "skipped_steps": []})
    _write(os.path.join(bf, "gate.json"), {"forge_feature": "boyfriend-chat-route"})
    # unlinked orphan -> Other
    lab = os.path.join(common, "fastship", "sessions", "lab-experiment")
    _write(os.path.join(lab, "orchestrator.json"),
           {"session_id": "lab-experiment", "requirement": "lab", "current_step": "1.4",
            "branch": "feat/lab", "completed_steps": [], "skipped_steps": []})
    _write(os.path.join(lab, "gate.json"), {})
    # 'default' placeholder -> excluded from Other
    _write(os.path.join(common, "fastship", "sessions", "default", "orchestrator.json"),
           {"session_id": "default", "current_step": None, "completed_steps": [], "skipped_steps": []})
    return tmp


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="/tmp/e2e_result.json")
    args = ap.parse_args()

    fixture = _build_fixture()
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(TOOLS, "forge_dashboard.py"),
         "--repo-root", fixture, "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    turns = []

    def turn(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"exception: {e}"
        turns.append({"turn": len(turns) + 1, "action": name, "observation": detail, "expect_ok": bool(ok)})

    try:
        up = False
        for _ in range(100):
            try:
                if _get(base + "/healthz")[0] == 200:
                    up = True; break
            except Exception:
                time.sleep(0.1)
        turn("dashboard server started (healthz 200)", lambda: (up, "server up" if up else "server NEVER reached healthz"))

        st, body = _get(base + "/api/state")
        snap = json.loads(body)
        turn("GET /api/state == 200 + non-empty north_star", lambda: (st == 200 and bool(snap.get("north_star")), f"north_star={snap.get('north_star')!r}"))

        objs = snap.get("objectives", [])
        ids = {o["id"] for o in objs}
        turn("all four forge objectives present (obj-1..obj-4)", lambda: ({"obj-1", "obj-2", "obj-3", "obj-4"} == ids, f"ids={sorted(ids)}"))
        turn("every objective overall_progress in [0,100]", lambda: (bool(objs) and all(0 <= o["rollup"]["overall_progress"] <= 100 for o in objs), f"checked {len(objs)} objectives"))

        # ---- "big feature split into many roadmaps": obj-4 grouped + rolled up correctly ----
        obj4 = next((o for o in objs if o["id"] == "obj-4"), None)
        obj4_slugs = {f["slug"] for f in obj4["features"]} if obj4 else set()
        turn("obj-4 groups exactly the 7 split features", lambda: (obj4_slugs == set(SPLIT), f"slugs={sorted(obj4_slugs)}"))
        turn("obj-4 rollup.total == 7", lambda: (bool(obj4) and obj4["rollup"]["total"] == 7, f"total={obj4['rollup']['total'] if obj4 else None}"))

        def _todo_ok():
            todo = obj4["rollup"]["todo"] if obj4 else []
            ok = len(todo) == 7 and all(t.get("slug") and t.get("status") == "draft" and "feature_progress" in t for t in todo)
            return ok, f"todo_n={len(todo)} sample={todo[0] if todo else None}"
        turn("obj-4 TODO = all 7 drafts, each well-formed (slug/status/progress)", _todo_ok)
        turn("obj-4 overall_progress == 0 (all drafts) - rollup math correct", lambda: (bool(obj4) and obj4["rollup"]["overall_progress"] == 0.0, f"overall={obj4['rollup']['overall_progress'] if obj4 else None}"))

        # ---- concluded feature reaches 100% via status mapping ----
        obj1 = next((o for o in objs if o["id"] == "obj-1"), None)
        admin = next((f for f in obj1["features"] if f["slug"] == "admin-stats"), None) if obj1 else None
        turn("concluded feature feature_progress == 100 + harvest attached", lambda: (bool(admin) and admin["feature_progress"] == 100.0 and admin["harvest"]["verdict"] == "achieved", f"prog={admin['feature_progress'] if admin else None}"))

        # ---- feature <-> fastship session linkage: telegram-binding links + step math (8/17) ----
        tg = next((f for f in obj1["features"] if f["slug"] == "telegram-binding"), None) if obj1 else None
        def _link_ok():
            if not tg or not tg.get("fastship"):
                return False, "telegram-binding not linked to a session"
            fs = tg["fastship"]
            ok = (fs["session_id"] == "telegram-binding" and fs["forge_feature"] == "telegram-binding"
                  and fs["current_step"] == "2.0" and fs["completed_count"] == 8
                  and fs["applicable_steps"] == 17 and fs["test_passed"] is True)
            return ok, f"sid={fs['session_id']} step={fs['current_step']} {fs['completed_count']}/{fs['applicable_steps']}"
        turn("telegram-binding feature links to its fastship session, step math 8/17", _link_ok)

        # ---- linkage contract holds for EVERY linked feature ----
        linked = [f for o in objs for f in o["features"] if f.get("fastship")]
        def _links_coherent():
            if not linked:
                return False, "no feature linked to any fastship session"
            for f in linked:
                fs, slug = f["fastship"], f["slug"]
                justified = (fs["session_id"] == slug or fs.get("forge_feature") == slug
                             or fs["session_id"].startswith(slug + "-"))
                coherent = (fs["current_step"] is not None
                            and 0 <= fs["completed_count"] <= fs["applicable_steps"]
                            and fs["applicable_steps"] > 0)
                if not (justified and coherent):
                    return False, f"bad link slug={slug} sid={fs['session_id']}"
            return True, f"{len(linked)} link(s) justified+coherent"
        turn("every feature<->session link justified by slug rule AND step-coherent", _links_coherent)

        # ---- R1: per-feature branch/worktree (real worktree fallback + live-over-stale) ----
        allfeats = [f for o in objs for f in o["features"]]
        turn("every feature exposes branch+worktree keys (— when unknown)",
             lambda: (all("branch" in f and "worktree" in f for f in allfeats), f"n={len(allfeats)}"))
        img = next((f for f in allfeats if f["slug"] == "persona-image-generator"), None)
        turn("no-session feature resolves branch via REAL git worktree fallback",
             lambda: (bool(img and img.get("worktree") == "persona-image-generator" and img.get("branch") == "feat/img-gen"),
                      f"wt={img and img.get('worktree')} br={img and img.get('branch')}"))
        bf = next((f for f in allfeats if f["slug"] == "boyfriend-chat-route"), None)
        turn("worktree-session feature prefers LIVE porcelain branch over stale recorded branch",
             lambda: (bool(bf and bf.get("worktree") == "wt-bf" and bf.get("branch") == "feat/bf-live"),
                      f"wt={bf and bf.get('worktree')} br={bf and bf.get('branch')}"))
        # ---- R2: Other = unlinked-only (excludes linked + stale dup + default) ----
        oth = snap.get("other_sessions", [])
        oids = {s["session_id"] for s in oth}
        turn("other_sessions = unlinked only (lab-experiment in; linked+default+dup out)",
             lambda: ("lab-experiment" in oids and "telegram-binding" not in oids
                      and "boyfriend-chat-route" not in oids and "default" not in oids
                      and "telegram-binding-old" not in oids, f"other={sorted(oids)}"))
        lab = next((s for s in oth if s["session_id"] == "lab-experiment"), None)
        turn("Other session carries fastship progress + branch (no metric)",
             lambda: (bool(lab and lab.get("branch") == "feat/lab" and "step_progress" in lab), f"lab={lab and lab.get('branch')}"))

        sh, html = _get(base + "/")
        turn("GET / serves dashboard HTML that fetches /api/state", lambda: (sh == 200 and "/api/state" in html and "<!DOCTYPE html>" in html, "html ok"))
        turn("HTML wires otherCard + wtLine with em-dash fallback",
             lambda: (all(k in html for k in ("other_sessions", "otherCard", "wtLine")) and 'o.branch||"—"' in html, "html wired"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(fixture, ignore_errors=True)

    passed = all(t["expect_ok"] for t in turns)
    result = {"scenario": "forge_dashboard_e2e", "turns": turns,
              "passed": passed, "turn_count": len(turns),
              # nested mirror so the fastship orchestrator e2e-report validator
              # (scenarios[].rounds[].turns) counts turns; flat keys above serve forge_dashboard_gate.
              "scenarios": [{"name": "forge_dashboard_e2e", "rounds": [{"turns": turns}]}]}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
