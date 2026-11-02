import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import FIX, SCRIPTS

CLI = SCRIPTS / "router.py"
FAKE = Path(__file__).resolve().parent / "fake_claude.py"

GH_SHIM = r'''#!/usr/bin/env python3
import json, os, sys, pathlib
log = pathlib.Path(os.environ["GH_LOG"]); args = sys.argv[1:]
with log.open("a") as f: f.write(json.dumps(args, ensure_ascii=False) + "\n")
n = sum(1 for l in log.read_text().splitlines() if '"create"' in l)
canned = json.loads(pathlib.Path(os.environ["GH_CANNED"]).read_text()) if os.environ.get("GH_CANNED") else {}
for key, out in canned.items():
    if " ".join(args).startswith(key): print(out); sys.exit(0)
if args[:2] == ["issue", "list"] or args[:2] == ["pr", "list"]: print("[]")
elif args[:2] == ["issue", "create"]: print(f"https://github.com/o/r/issues/{100 + n}")
elif args[0] == "api" and "--jq" in args: print("9" + args[1].rsplit("/", 1)[-1])
'''


def git_project(tmp_path, root):
    """root = 一个 clone，origin 是本地裸仓，带 staging 分支（dispatch 要 fetch + worktree add）。"""
    g = lambda *a, cwd=None: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
    origin = tmp_path / "origin.git"
    g("init", "-q", "--bare", "-b", "staging", str(origin))
    g("clone", "-q", str(origin), str(root))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        g("config", k, v, cwd=root)
    (root / ".gitignore").write_text(".claude/\n")
    g("add", ".gitignore", cwd=root); g("commit", "-qm", "init", cwd=root); g("push", "-q", "origin", "HEAD:staging", cwd=root)


def wait_until(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    raise AssertionError("timeout")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    root = tmp_path / "proj"; home = root / ".claude" / "router"
    git_project(tmp_path, root)
    home.mkdir(parents=True)
    runs = tmp_path / "daily" / "runs"; (runs / "2026-10-26").mkdir(parents=True)
    (tmp_path / "daily" / "state").mkdir()
    bin_ = tmp_path / "bin"; bin_.mkdir()
    (bin_ / "gh").write_text(GH_SHIM); (bin_ / "gh").chmod(0o755)
    cfg = {"repo": "o/r", "base_branch": "staging", "worktree_dir": ".claude/worktrees",
           "map_title": "her 周报问题", "weekly_runs_dir": str(runs),
           "lark_config": str(tmp_path / "no-lark.json"),
           "leak_guard_globs": [str(runs / "*" / "users.json")],
           "slots": {"heavy": 3, "light": 2}, "kickoff_budget_min": 30, "stale_min": 45,
           "disk_limit_gb": 150,
           "worker": {"model": "claude-opus-5-5", "permission_mode": "bypassPermissions",
                      "allowed_tools": [], "disallowed_tools": ["Bash(gh pr merge*)"],
                      "claude_bin": [sys.executable, str(FAKE)]}}
    (home / "config.json").write_text(json.dumps(cfg))
    shutil.copy(FIX / "surfaces.json", home / "surfaces.json")
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_LOG", str(tmp_path / "claude.log"))
    monkeypatch.delenv("ROUTER_HOME", raising=False); monkeypatch.delenv("ROUTER_TICKET", raising=False)

    class P:
        pass
    p = P(); p.root, p.home, p.tmp, p.runs = root, home, tmp_path, runs

    def run(*args, env=None, check=True):
        r = subprocess.run([sys.executable, str(CLI), "--root", str(root), *args],
                           capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=30)
        if check:
            assert r.returncode == 0, r.stdout + r.stderr
        return r
    p.run = run
    p.out = lambda *a, **k: json.loads(run(*a, **k).stdout)
    p.state = lambda: json.loads((home / "state.json").read_text())
    p.gh = lambda: [json.loads(l) for l in (tmp_path / "gh.log").read_text().splitlines()] if (tmp_path / "gh.log").exists() else []
    p.claude = lambda: [json.loads(l) for l in (tmp_path / "claude.log").read_text().splitlines()] if (tmp_path / "claude.log").exists() else []
    return p


def seed(p, **tickets):
    """直接写一份 state（跳过 draft/start），给 answer / takeover 这类测试用。"""
    from conftest import make_state
    s = make_state(**tickets)
    (p.home / "state.json").write_text(json.dumps(s, ensure_ascii=False))
    return s


def test_draft_creates_tickets_without_touching_github(proj):
    out = proj.out("draft", "--from", str(FIX / "triage.json"))
    assert out["ok"] and out["tickets"] == ["T1", "T2", "T3", "T4"]
    s = proj.state()
    assert [s["tickets"][t]["status"] for t in ("T1", "T2", "T3", "T4")] == ["draft", "draft", "hold", "skip"]
    assert s["tickets"]["T2"]["blocked_by"] == ["T1"] and s["tickets"]["T2"]["weight"] == "heavy"
    assert s["tickets"]["T1"]["solution"] == {"kind": "clue", "text": "#940 第 4 条", "source": "router 分诊"}
    assert s["tickets"]["T4"]["waiting_for"] == "已由 feat/her-attention-by-stage 覆盖：已在跑"
    assert s["batch"] == {"id": "2026-10-26", "started": False, "started_at": None}
    assert [(q["kind"], q["summary"]) for q in s["queue"]] == [("start", "派发单已备好（2 张）")]
    assert proj.gh() == [] and proj.claude() == []
    assert "DRY RUN" in out["notify"]
    assert "派发单已备好（2 张）" in (proj.home / "board.html").read_text()


def test_draft_same_batch_twice_refuses_without_replace(proj):
    proj.out("draft", "--from", str(FIX / "triage.json"))
    r = proj.run("draft", "--from", str(FIX / "triage.json"), check=False)
    assert r.returncode == 1 and "已有草稿" in json.loads(r.stdout)["error"]
    out = proj.out("draft", "--from", str(FIX / "triage.json"), "--replace")
    assert out["tickets"] == ["T5", "T6", "T7", "T8"] and set(proj.state()["tickets"]) == {"T5", "T6", "T7", "T8"}


def test_start_subset_creates_issues_and_queues_only_selected(proj):
    proj.out("draft", "--from", str(FIX / "triage.json"))
    out = proj.out("start", "T1")
    s = proj.state()
    assert out["started"] == ["T1"] and s["tickets"]["T1"]["start_ok"]
    assert s["tickets"]["T2"]["status"] == "draft" and s["batch"]["started"] is True
    creates = [c for c in proj.gh() if c[:2] == ["issue", "create"]]
    assert [c[c.index("--title") + 1] for c in creates] == ["her 周报问题", s["tickets"]["T1"]["title"]]
    assert s["tickets"]["T1"]["issue"] == 102
    assert [q["kind"] for q in s["queue"]] == ["start"]        # 还有没 start 的票，派发单留着
    assert out["dispatched"] == ["T1"] and s["tickets"]["T1"]["status"] == "kickoff"
    assert (proj.root / ".claude/worktrees/router-T1").is_dir()
    wait_until(lambda: proj.claude())
    brief = proj.claude()[0][proj.claude()[0].index("-p") + 1]
    assert "worker.md" in brief and "#102" in brief


def test_start_all_links_dependencies_and_clears_start_item(proj):
    proj.out("draft", "--from", str(FIX / "triage.json"))
    proj.out("add", "--title", "临时需求", "--problem", "p", "--surfaces", "llm-config")
    out = proj.out("start", "--all")
    assert out["started"] == ["T1", "T2"]                       # 用户临时加的 T5 要单独 start
    s = proj.state()
    assert s["tickets"]["T5"]["status"] == "draft" and s["queue"] == []
    assert ["api", "--method", "POST", "repos/o/r/issues/103/dependencies/blocked_by", "-F", "issue_id=9102"] in proj.gh()


def test_start_blocks_when_issue_body_leaks_uid(proj):
    (proj.runs / "2026-10-26" / "users.json").write_text(json.dumps({"5eed0001": {}}))
    tri = json.loads((FIX / "triage.json").read_text())
    tri["tickets"][1]["why"]["evidence"] = "见用户 5eed0001"
    (proj.tmp / "t.json").write_text(json.dumps(tri, ensure_ascii=False))
    proj.out("draft", "--from", str(proj.tmp / "t.json"))
    r = proj.run("start", "T1", "T2", check=False)
    out = json.loads(r.stdout)
    assert r.returncode == 1 and out["blocked"] == [{"ticket": "T2", "hits": ["5eed0001"]}]
    s = proj.state()
    assert s["tickets"]["T2"]["status"] == "draft" and s["tickets"]["T1"]["status"] == "kickoff"
    assert not any("5eed0001" in json.dumps(c) for c in proj.gh())


def test_add_creates_user_draft(proj):
    out = proj.out("add", "--title", "临时需求", "--metric", "m", "--users", "3", "--evidence", "e",
                   "--problem", "p", "--anchor", "a.rs:1", "--clue", "c", "--surfaces", "her-reply-prompt,llm-config",
                   "--tags", "x,y")
    t = proj.state()["tickets"][out["ticket"]]
    assert (t["origin"], t["status"], t["weight"], t["surfaces"], t["tags"]) == \
        ("user", "draft", "heavy", ["her-reply-prompt", "llm-config"], ["x", "y"])


def test_submit_card_queues_card_and_shows_card_solution(proj):
    seed(proj, T1=("kickoff", ["weekly-report"], {"title": "周报口径"}), T2=("queued", [], {"blocked_by": ["T1"]}))
    card = proj.home / "cards" / "T1.md"; card.parent.mkdir()
    card.write_text("## 方向卡\n**1. 问题**\nx\n**3. 方案方向**\n- 拆 tells 为三项\n- 加口径版本号\n**4. 为什么不选另一种做法**\ny\n")
    out = proj.out("submit-card", "T1", "--card", str(card), "--rounds", "1", "--overtime")
    s = proj.state(); t = s["tickets"]["T1"]
    assert t["status"] == "await_card" and t["solution"]["kind"] == "card"
    assert "拆 tells 为三项" in t["solution"]["text"] and "为什么不选" not in t["solution"]["text"]
    assert t["solution"]["source"] == "方向卡 v1（待你确认）"
    q = s["queue"][0]
    assert (q["id"], q["kind"], q["ticket"], q["blocks"]) == (out["queue"], "card", "T1", 1) and "超时" in q["summary"]


def test_answer_resumes_worker_with_verbatim_text(proj):
    wt = proj.tmp / "wt"; wt.mkdir()
    s = seed(proj, T1=("await_card", ["weekly-report"], {
        "solution": {"kind": "card", "text": "拆 tells", "source": "方向卡 v1（待你确认）"},
        "worker": {"session_id": "sid-1", "worktree": str(wt), "runs": 1}}))
    s["queue"] = [{"id": "Q1", "ticket": "T1", "kind": "card", "summary": "x", "blocks": 0, "created_at": "t"}]
    (proj.home / "state.json").write_text(json.dumps(s))
    from events import deliver
    deliver(proj.home, "T1", {"type": "interface_changed", "ticket": "T2", "summary": "底座前缀改名"})
    out = proj.out("answer", "Q1", "C 改：先只拆 tells，安全指标下周")
    assert out["resumed"] == "T1"
    wait_until(lambda: proj.claude())
    argv = proj.claude()[0]
    assert argv[argv.index("--resume") + 1] == "sid-1"
    prompt = argv[argv.index("-p") + 1]
    assert "C 改：先只拆 tells，安全指标下周" in prompt and "底座前缀改名" in prompt
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "executing" and t["solution"]["source"] == "方向卡 v1（已确认）"
    assert t["worker"]["pid"] and t["worker"]["log"].endswith("T1-2.log") and proj.state()["queue"] == []
    assert "C 改：先只拆 tells，安全指标下周" in (proj.home / "answers.md").read_text()


def test_takeover_prints_resume_command_and_stops_scheduling(proj, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    wt = proj.tmp / "wt"; wt.mkdir()
    p = subprocess.Popen([sys.executable, str(FAKE)], start_new_session=True)
    seed(proj, T1=("executing", ["weekly-report"], {"worker": {"session_id": "sid-1", "worktree": str(wt), "pid": p.pid}}))
    out = proj.out("takeover", "T1")
    assert out["command"] == f"cd {wt} && claude --resume sid-1"
    p.wait(timeout=10)                                          # 接管前先停掉后台 worker
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "takeover" and t["worker"]["pid"] is None
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "0")
    out = proj.out("handback", "T1")
    t = proj.state()["tickets"]["T1"]
    assert out["status"] == "executing" and t["status"] == "executing" and t["worker"]["pid"]
    wait_until(lambda: any("--resume" in a for a in proj.claude()))   # 第一条是被停掉的那个假 worker
    argv = next(a for a in proj.claude() if "--resume" in a)
    assert argv[argv.index("--resume") + 1] == "sid-1"


def test_tell_pause_stop_prioritize(proj):
    seed(proj, T1=("executing", ["weekly-report"]))
    proj.out("tell", "T1", "先别动 releases.json")
    proj.out("pause", "T1")
    inbox = [json.loads(l) for l in (proj.home / "inbox" / "T1.jsonl").read_text().splitlines()]
    assert [(m["type"], m["summary"]) for m in inbox] == [("tell", "先别动 releases.json"), ("pause", "先停：做完当前这一步就停")]
    assert proj.state()["tickets"]["T1"]["pause_requested"] is True
    proj.out("stop", "T1"); assert proj.state()["tickets"]["T1"]["status"] == "paused"
    proj.out("prioritize", "T1", "0"); assert proj.state()["tickets"]["T1"]["priority"] == 0


def test_decide_and_revoke(proj):
    seed(proj, T1=("executing", ["a"]))
    d = proj.out("decide", "--ticket", "T1", "--decision", "排在 T2 之后", "--why", "同改 prompt", "--basis", "surfaces.json")["decision"]
    assert d == "D1"
    proj.out("revoke", "D1", "--reason", "用户推翻")
    assert "D1 已撤回" in (proj.home / "decisions.md").read_text()
    s = proj.state()
    assert s["tickets"]["T1"]["review_flag"] is True
    assert "D1 已撤回" in (proj.home / "inbox" / "T1.jsonl").read_text()


def test_emit_from_worker_env_writes_event(proj):
    env = {"ROUTER_TICKET": "T1", "ROUTER_HOME": str(proj.home)}
    out = proj.out("emit", "interface_changed", "底座前缀改名", "--evidence", "prompt_template_en.rs:48",
                   "--surfaces", "her-reply-prompt", env=env)
    assert out["ok"] and out["event"]["stop"] is False
    proj.out("progress", "card_ready", env=env)
    lines = [json.loads(l) for l in (proj.home / "events" / "T1.jsonl").read_text().splitlines()]
    assert [l["type"] for l in lines] == ["interface_changed", "progress"]
    assert lines[0]["affects"]["surfaces"] == ["her-reply-prompt"]
    bad = proj.run("emit", "progress", "x", "--evidence", "y", env=env, check=False)
    assert bad.returncode == 2 and json.loads(bad.stdout)["ok"] is False


def test_inbox_reads_once(proj):
    seed(proj, T1=("executing", ["a"]))
    proj.out("tell", "T1", "hello")
    env = {"ROUTER_TICKET": "T1", "ROUTER_HOME": str(proj.home)}
    assert [m["summary"] for m in proj.out("inbox", env=env)["messages"]] == ["hello"]
    assert proj.out("inbox", env=env)["messages"] == []


def test_walkthrough_prod_batch_and_released(proj):
    releases = proj.tmp / "daily" / "state" / "releases.json"
    old = [{"date": "2026-10-14", "what": "#905", "metrics": ["photo"]}]
    releases.write_text(json.dumps(old, ensure_ascii=False))
    s = seed(proj, T1=("merged_staging", ["a"]), T2=("merged_staging", ["b"]))
    s["queue"] = [{"id": "Q1", "ticket": "T1", "kind": "handson", "summary": "staging 走查 T1", "blocks": 0, "created_at": "t"}]
    s["seq"]["queue"] = 1
    (proj.home / "state.json").write_text(json.dumps(s))
    proj.out("walkthrough-done", "T1")
    st = proj.state(); assert st["tickets"]["T1"]["status"] == "await_prod" and st["queue"] == []
    proj.out("walkthrough-done", "T2")
    out = proj.out("prod-batch")
    st = proj.state(); assert [(q["kind"], q["summary"]) for q in st["queue"]] == [("prod", "上 prod：T1 T2")]
    proj.out("released", "--tickets", "T1", "T2", "--what", "#941 承认是 AI", "--metrics", "claims_human,offline_meet")
    rel = json.loads(releases.read_text())
    assert rel[0] == old[0] and rel[1]["what"] == "#941 承认是 AI" and rel[1]["metrics"] == ["claims_human", "offline_meet"]
    st = proj.state(); assert {st["tickets"][t]["status"] for t in ("T1", "T2")} == {"done"} and st["queue"] == []


def test_status_text_and_json(proj):
    proj.out("draft", "--from", str(FIX / "triage.json"))
    txt = proj.run("status").stdout
    assert "T1" in txt and "派发单已备好" in txt
    assert proj.out("status", "--json")["counts"]["draft"] == 2


def test_mutating_commands_serialize_on_lock(proj):
    seed(proj, T1=("queued", ["a"]))
    with open(proj.home / ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        p = subprocess.Popen([sys.executable, str(CLI), "--root", str(proj.root), "prioritize", "T1", "5"],
                             stdout=subprocess.PIPE, text=True)
        time.sleep(0.5)
        assert p.poll() is None                     # 被锁挡着，等 tick 写完再改
    assert p.wait(timeout=10) == 0 and proj.state()["tickets"]["T1"]["priority"] == 5


def test_corrupt_state_refuses_to_run(proj):
    (proj.home / "state.json").write_text("{broken")
    r = proj.run("status", check=False)
    assert r.returncode == 1 and "解析失败" in json.loads(r.stdout)["error"]
    assert (proj.home / "state.json").read_text() == "{broken"


def test_watch_exits_when_there_is_something_for_router(proj):
    seed(proj, T1=("executing", ["a"], {"worker": {"pid": 999999, "session_id": "s", "worktree": str(proj.tmp),
                                                   "started_at": "2026-11-02T00:00:00Z"}}))
    out = proj.out("watch", "--interval", "1")
    assert out["woke"] == ["new_queue"] and proj.state()["tickets"]["T1"]["status"] == "stuck"
    assert proj.out("tick")["new_queue"] == []


def test_start_surfaces_dispatch_errors(proj):
    proj.out("draft", "--from", str(FIX / "triage.json"))
    subprocess.run(["git", "remote", "remove", "origin"], cwd=proj.root, check=True)
    r = proj.run("start", "T1", check=False)            # fetch 失败 → 开不了 worktree，要报出来
    out = json.loads(r.stdout)
    assert r.returncode == 1 and out["started"] == ["T1"] and out["errors"][0]["error"].startswith("dispatch T1")
    assert proj.state()["tickets"]["T1"]["status"] == "queued"


def test_submit_card_on_overtime_stops_live_worker(proj, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    p = subprocess.Popen([sys.executable, str(FAKE)], start_new_session=True)
    seed(proj, T1=("kickoff", ["a"], {"worker": {"pid": p.pid, "session_id": "s"}}))
    card = proj.tmp / "c.md"; card.write_text("**3. 方案方向**\n半张卡\n")
    proj.out("submit-card", "T1", "--card", str(card), "--overtime")
    assert p.wait(timeout=10) is not None                 # 超时交卡：后台 worker 先停，等你确认后再续跑


# ---- 终审修复 ----------------------------------------------------------------

def _queue(proj, s, *items):
    s["queue"] = [{"id": q, "ticket": t, "kind": k, "summary": "x", "blocks": 0, "created_at": "t"} for q, t, k in items]
    s["seq"]["queue"] = len(items)
    (proj.home / "state.json").write_text(json.dumps(s))


def test_resume_with_live_worker_only_delivers_inbox(proj, monkeypatch):   # C3
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    p = subprocess.Popen([sys.executable, str(FAKE)], start_new_session=True)
    s = seed(proj, T1=("executing", ["a"], {"worker": {"pid": p.pid, "session_id": "s", "worktree": str(proj.tmp)}}))
    _queue(proj, s, ("Q1", "T1", "conflict"))
    out = proj.out("answer", "Q1", "按 T1 来")
    assert out["resumed"] is None and out["delivered"] == "T1"
    assert proj.state()["tickets"]["T1"]["worker"]["pid"] == p.pid and p.poll() is None
    assert "按 T1 来" in (proj.home / "inbox" / "T1.jsonl").read_text()
    p.kill()


def test_answer_stuck_stops_live_worker_then_resumes(proj, monkeypatch):    # C3
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    p = subprocess.Popen([sys.executable, str(FAKE)], start_new_session=True)
    s = seed(proj, T1=("executing", ["a"], {"card_confirmed": True, "worker": {"pid": p.pid, "session_id": "s", "worktree": str(proj.tmp)}}))
    _queue(proj, s, ("Q1", "T1", "stuck"))
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "0")
    out = proj.out("answer", "Q1", "续跑")
    assert out["resumed"] == "T1" and p.wait(timeout=10) is not None
    assert proj.state()["tickets"]["T1"]["worker"]["pid"] != p.pid


def test_answer_reject_keeps_kickoff_and_asks_rewrite(proj):              # I3
    s = seed(proj, T1=("await_card", ["a"], {"solution": {"kind": "card", "text": "t", "source": "方向卡 v1（待你确认）"},
                                             "worker": {"session_id": "s", "worktree": str(proj.tmp)}}))
    _queue(proj, s, ("Q1", "T1", "card"))
    out = proj.out("answer", "Q1", "根因找错了，重新查", "--reject")
    t = proj.state()["tickets"]["T1"]
    assert out["resumed"] == "T1" and t["status"] == "kickoff" and not t.get("card_confirmed")
    assert t["solution"]["source"] == "方向卡 v1（被驳回）"
    wait_until(lambda: proj.claude())
    prompt = proj.claude()[0][proj.claude()[0].index("-p") + 1]
    assert "驳回" in prompt and "card_ready" in prompt and "已确认" not in prompt


def test_takeover_blocks_resume(proj):                                    # I6
    s = seed(proj, T1=("takeover", ["a"], {"takeover_from": "executing", "worker": {"session_id": "s", "worktree": str(proj.tmp)}}))
    _queue(proj, s, ("Q1", "T1", "direction"))
    r = proj.run("tell", "T1", "x", "--resume", check=False)
    assert r.returncode == 1 and "接管" in json.loads(r.stdout)["error"]
    out = proj.out("answer", "Q1", "y")
    assert out["resumed"] is None and proj.state()["tickets"]["T1"]["status"] == "takeover"
    assert proj.claude() == []


def test_unknown_surface_names_rejected(proj):                            # I7
    r = proj.run("add", "--title", "x", "--surfaces", "her-reply-promt", check=False)
    assert r.returncode == 1 and "her-reply-promt" in json.loads(r.stdout)["error"]
    tri = json.loads((FIX / "triage.json").read_text()); tri["tickets"][0]["surfaces"] = ["nope"]
    (proj.tmp / "t.json").write_text(json.dumps(tri, ensure_ascii=False))
    assert proj.run("draft", "--from", str(proj.tmp / "t.json"), check=False).returncode == 1


def test_surfaces_command_updates_and_logs_decision(proj):                # I7
    seed(proj, T1=("paused", ["weekly-report"]))
    out = proj.out("surfaces", "T1", "--set", "weekly-report,her-reply-prompt", "--why", "越界：要改底座")
    t = proj.state()["tickets"]["T1"]
    assert t["surfaces"] == ["weekly-report", "her-reply-prompt"] and t["weight"] == "heavy" and out["decision"] == "D1"
    assert "越界：要改底座" in (proj.home / "decisions.md").read_text()


def test_worker_env_cannot_run_router_only_commands(proj):
    seed(proj, T1=("draft", ["a"]))
    env = {"ROUTER_TICKET": "T1", "ROUTER_HOME": str(proj.home)}
    for args in (("start", "T1"), ("answer", "Q1", "放行"), ("decide", "--ticket", "T1", "--decision", "d", "--why", "w", "--basis", "b")):
        r = proj.run(*args, env=env, check=False)
        assert r.returncode == 2 and "worker" in json.loads(r.stdout)["error"]


def test_submit_card_replaces_older_card_item_and_requires_kickoff(proj):
    s = seed(proj, T1=("kickoff", ["a"]), T2=("executing", ["b"]))
    _queue(proj, s, ("Q1", "T1", "card"))
    card = proj.tmp / "c.md"; card.write_text("x")
    proj.out("submit-card", "T1", "--card", str(card))
    assert [q["kind"] for q in proj.state()["queue"] if q["ticket"] == "T1"] == ["card"]
    assert proj.state()["queue"][0]["id"] != "Q1"
    assert proj.run("submit-card", "T2", "--card", str(card), check=False).returncode == 1
