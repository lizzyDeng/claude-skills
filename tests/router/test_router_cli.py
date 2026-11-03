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
for key, out in canned.items():                  # 值可以是字符串，或 {"out", "rc", "err"}
    if " ".join(args).startswith(key):
        if isinstance(out, dict):
            print(out.get("out", "")); print(out.get("err", ""), file=sys.stderr); sys.exit(out.get("rc", 0))
        print(out); sys.exit(0)
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
    assert "notify" not in out and s["queue"][0]["notified_at"] is None     # R4：draft 不直发，下一轮 tick 推 Lark
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
    assert (q["id"], q["kind"], q["ticket"]) == (out["queue"], "card", "T1") and "超时" in q["summary"]
    view = next(x for x in proj.out("status", "--json")["queue"] if x["id"] == q["id"])
    assert view["blocks"] == 2                                  # R3：读时现算 = 自己 1 张 + 依赖它的 T2


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


def test_start_refuses_before_creating_issues_when_base_unreachable(proj):   # ②
    proj.out("draft", "--from", str(FIX / "triage.json"))
    subprocess.run(["git", "remote", "remove", "origin"], cwd=proj.root, check=True)
    r = proj.run("start", "T1", check=False)            # 查不了 base ⇒ 在任何 GitHub 写入之前就停
    out = json.loads(r.stdout)
    assert r.returncode == 1 and "staging" in json.dumps(out, ensure_ascii=False)
    assert not [c for c in proj.gh() if c[:2] == ["issue", "create"]]       # 连 map 都不建
    assert proj.state()["tickets"]["T1"]["status"] == "draft"


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


# =============================================================================
# plan v4 + 用户裁决 H1–H5：先红用例（CLI 层）
# =============================================================================
import re  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

from conftest import make_state  # noqa: E402


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _write_state(proj, s):
    (proj.home / "state.json").write_text(json.dumps(s, ensure_ascii=False))


def _item(qid, ticket, kind, topic=None, **kw):
    q = {"id": qid, "ticket": ticket, "kind": kind, "summary": "x", "created_at": "t", "notified_at": "t", **kw}
    if topic:
        q["topic"] = topic
    return q


def _set_queue(proj, s, *items):
    s["queue"] = list(items)
    s["seq"]["queue"] = len(items)
    _write_state(proj, s)


def _cfg(proj, **kw):
    p = proj.home / "config.json"
    cfg = json.loads(p.read_text()); cfg.update(kw); p.write_text(json.dumps(cfg))
    return cfg


def _canned(proj, monkeypatch, mapping):
    f = proj.tmp / "canned.json"; f.write_text(json.dumps(mapping, ensure_ascii=False))
    monkeypatch.setenv("GH_CANNED", str(f))


def _view(state="OPEN", *bodies):
    return json.dumps({"state": state, "comments": [{"body": b} for b in bodies]}, ensure_ascii=False)


def _writes(gh):
    return [c for c in gh if c[:2] in (["issue", "comment"], ["issue", "close"], ["issue", "create"],
                                       ["issue", "edit"], ["pr", "edit"], ["pr", "merge"])]


def _branch_exists(proj, b):
    return bool(git("branch", "--list", b, cwd=proj.root).strip())


# ---- R1：改回 PR 的回答不唤醒 worker；start 先查 base ------------------------------

def test_answer_retarget_item_reverifies_without_waking_worker(proj):    # A3 #10
    wt = proj.tmp / "wt"; wt.mkdir()
    s = seed(proj, T1=("verifying", ["weekly-report"], {"pr": 41, "card_confirmed": True, "wrong_base_reported": 41,
                                                        "retarget_fail": {"count": 2, "qid": "Q1"},
                                                        "worker": {"session_id": "sid-1", "worktree": str(wt), "runs": 2}}))
    _set_queue(proj, s, _item("Q1", "T1", "stuck", topic="retarget", summary="PR #41 改回 staging 失败"))
    out = proj.out("answer", "Q1", "改好了")
    time.sleep(0.5)
    assert proj.claude() == [] and proj.gh() == []                     # 不续跑 worker，也不碰网络
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "verifying" and "retarget_fail" not in t and "wrong_base_reported" not in t
    assert "下一轮重验" in out["hint"]


def _origin_with_main_without_staging(proj, reject_staging=False):
    """origin 有 main、没有 staging；clone 里还留着 refs/remotes/origin/staging（「见过」）。"""
    origin = proj.tmp / "origin.git"
    git("push", "-q", "origin", "HEAD:main", cwd=proj.root)
    git("fetch", "-q", "origin", cwd=proj.root)
    git("update-ref", "-d", "refs/heads/staging", cwd=origin)
    assert git("rev-parse", "--verify", "--quiet", "refs/remotes/origin/staging", cwd=proj.root).strip()
    if reject_staging:
        hook = origin / "hooks" / "pre-receive"
        hook.write_text('#!/bin/sh\nwhile read old new ref; do\n  [ "$ref" = "refs/heads/staging" ] && '
                        '{ echo "staging is protected" >&2; exit 1; }\ndone\nexit 0\n')
        hook.chmod(0o755)
    return origin


def test_start_rebuild_push_rejected_queues_and_creates_no_issue(proj):   # A3 #1
    proj.out("draft", "--from", str(FIX / "triage.json"))
    _origin_with_main_without_staging(proj, reject_staging=True)
    before = len(proj.state()["queue"])
    r = proj.run("start", "T1", check=False)
    assert r.returncode == 1
    assert not [c for c in proj.gh() if c[:2] == ["issue", "create"]]
    s = proj.state()
    assert s["tickets"]["T1"]["status"] == "draft"
    base = [q for q in s["queue"] if q.get("topic") == "base"]
    assert len(s["queue"]) == before + 1 and len(base) == 1          # 决定单项先落盘，再 Fail
    d = proj.home / "decisions.md"
    assert not (d.exists() and "重建" in d.read_text())


def test_start_rebuilds_missing_base_then_dispatches(proj):
    proj.out("draft", "--from", str(FIX / "triage.json"))
    origin = _origin_with_main_without_staging(proj)
    hook = proj.root / ".git" / "hooks" / "pre-push"                  # 主 checkout 的 pre-push 拒一切：证明走了 --no-verify
    hook.write_text("#!/bin/sh\necho 'pre-push: dirty tree' >&2\nexit 1\n"); hook.chmod(0o755)
    out = proj.out("start", "T1")
    assert git("rev-parse", "refs/heads/staging", cwd=origin) == git("rev-parse", "refs/heads/main", cwd=origin)
    assert out["dispatched"] == ["T1"] and proj.state()["tickets"]["T1"]["status"] == "kickoff"
    lines = [l for l in (proj.home / "decisions.md").read_text().splitlines() if "重建" in l]
    assert len(lines) == 1


# ---- R2：start 的派发错误带票号和阶段（③⑤） -----------------------------------------

def test_start_dispatch_error_names_ticket(proj):
    proj.out("draft", "--from", str(FIX / "triage.json"))
    wt = proj.root / ".claude" / "worktrees" / "router-T1"
    wt.parent.mkdir(parents=True); wt.write_text("not a dir")          # worktree 路径上先放一个普通文件
    r = proj.run("start", "T1", check=False)
    out = json.loads(r.stdout)
    assert r.returncode == 1 and len(out["errors"]) == 1
    e = out["errors"][0]
    assert set(e) == {"ticket", "phase", "error"} and (e["ticket"], e["phase"]) == ("T1", "worktree")
    assert e["error"].startswith("git worktree")


def test_start_spawn_failure_names_ticket_and_phase(proj):
    proj.out("draft", "--from", str(FIX / "triage.json"))
    cfg = json.loads((proj.home / "config.json").read_text())
    cfg["worker"]["claude_bin"] = str(proj.tmp / "no-such-claude")   # 真 FileNotFoundError
    (proj.home / "config.json").write_text(json.dumps(cfg))
    r = proj.run("start", "T1", check=False)
    out = json.loads(r.stdout)                                          # 不是 traceback：stdout 仍是 JSON
    assert r.returncode == 1 and (out["errors"][0]["ticket"], out["errors"][0]["phase"]) == ("T1", "spawn")
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "queued" and t["dispatch_fail"]["count"] == 1


# ---- R3：决定单的「几张 / 挡几张」读时现算（④⑧） ------------------------------------

def test_partial_start_refreshes_start_item(proj):
    proj.out("draft", "--from", str(FIX / "triage.json"))              # 2 张 dispatch 草稿
    proj.out("start", "T1")
    start = [q for q in proj.out("status", "--json")["queue"] if q["kind"] == "start"]
    assert len(start) == 1 and "（1 张）" in start[0]["summary"] and start[0]["blocks"] == 1


def test_status_derives_queue_counts_from_stale_legacy_state(proj):     # 复刻现场：Q1 存着「4 张 / 挡 4」
    s = seed(proj, T3=("draft", ["weekly-report"], {"batch": "2026-10-26"}),
             T4=("draft", ["llm-config"], {"batch": "2026-10-26"}), T5=("draft", [], {"batch": "2026-10-26"}))
    s["batch"] = {"id": "2026-10-26", "started": True, "started_at": "2026-11-02T13:24:58Z"}
    s["queue"] = [{"id": "Q1", "ticket": None, "kind": "start", "summary": "派发单已备好（4 张）", "blocks": 4,
                   "created_at": "2026-11-02T09:00:00Z"}]                  # 老格式：没有 notified_at
    s["seq"]["queue"] = 1
    _write_state(proj, s)
    before = (proj.home / "state.json").read_bytes()
    txt = proj.run("status").stdout
    assert "派发单已备好（3 张）（挡 3 张）" in txt
    assert (proj.home / "state.json").read_bytes() == before                # 只读命令不写盘


def test_card_item_blocks_its_own_ticket(proj):
    seed(proj, T1=("kickoff", ["weekly-report"]))
    card = proj.tmp / "c.md"; card.write_text("**3. 方案方向**\n- x\n")
    qid = proj.out("submit-card", "T1", "--card", str(card))["queue"]
    q = next(x for x in proj.out("status", "--json")["queue"] if x["id"] == qid)
    assert q["blocks"] == 1


# ---- R4：命令行写进决定单的项，由下一轮 tick 推 Lark（⑦） ---------------------------

@pytest.fixture
def lark(proj, monkeypatch):
    posts = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = b'{"code":0}'
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    f = proj.tmp / "lark.json"; f.write_text(json.dumps({"webhook_url": f"http://127.0.0.1:{srv.server_port}/hook"}))
    _cfg(proj, lark_config=str(f))
    yield posts
    srv.shutdown()


def test_cli_items_are_notified_by_next_tick_once(proj, lark):
    seed(proj, T1=("kickoff", ["weekly-report"]))
    card = proj.tmp / "c.md"; card.write_text("**3. 方案方向**\n- x\n")
    qid = proj.out("submit-card", "T1", "--card", str(card))["queue"]
    assert lark == []                                                    # 前台命令不发
    proj.out("tick")
    assert len(lark) == 1 and lark[0]["content"]["text"] == f"router：等你 1 件（{qid}）"
    proj.out("tick")
    assert len(lark) == 1                                                # 发过的不再发
    s = proj.state()
    s["tickets"]["T2"] = {**make_state(T2=("await_prod", ["llm-config"]))["tickets"]["T2"]}
    s["seq"]["ticket"] = 2
    _write_state(proj, s)
    proj.out("prod-batch")
    assert len(lark) == 1
    proj.out("tick")
    assert len(lark) == 2


# ---- R5：续跑后 waiting_for 不被清空（⑥） --------------------------------------------

def test_answer_and_tell_resume_keep_waiting_for(proj):
    wt = proj.tmp / "wt"; wt.mkdir()
    s = seed(proj, T1=("await_card", ["weekly-report"], {
                 "solution": {"kind": "card", "text": "t", "source": "方向卡 v1（待你确认）"},
                 "worker": {"session_id": "s1", "worktree": str(wt), "runs": 1}}),
             T2=("kickoff", ["llm-config"], {"worker": {"session_id": "s2", "worktree": str(wt), "runs": 1, "pid": None}}))
    _set_queue(proj, s, _item("Q1", "T1", "card"))
    proj.out("answer", "Q1", "同意")
    w1 = proj.state()["tickets"]["T1"]["waiting_for"]
    assert w1 and "执行" in w1
    proj.out("tell", "T2", "把口径写清楚", "--resume")
    assert proj.state()["tickets"]["T2"]["waiting_for"]


# ---- R6：仓外票核对 → 完成 → tick 逐目标收尾（⑪⑫） -----------------------------------

def _live_guard(proj):
    """防泄漏闸要有文件可读（闸失效 = GitHub 各栏 fail-closed）：放一份合成的 users.json。"""
    (proj.runs / "2026-10-26" / "users.json").write_text(json.dumps({"5eed0001": {}}))


def _offrepo_await_check(proj, wt, **extra):
    _live_guard(proj)
    s = seed(proj, T1=("await_check", ["weekly-report"], {
                 "issue": 944, "title": "周报口径", "card_confirmed": True, "merge_sha": None,
                 "worker": {"session_id": "sid-t1-closeout", "worktree": str(wt) if wt else None,
                            "branch": "router/T1", "runs": 2}}), **extra)
    _set_queue(proj, s, _item("Q1", "T1", "handson", topic="offrepo", summary="核对 T1 的仓外改动"))
    return s


def _wt_for_t1(proj):
    wt = proj.root / ".claude" / "worktrees" / "router-T1"
    git("worktree", "add", "-q", "-b", "router/T1", str(wt), "origin/staging", cwd=proj.root)
    return wt


def test_answer_handson_offrepo_finishes_and_tick_closes_issue_and_branch(proj, monkeypatch):
    wt = _wt_for_t1(proj)
    _offrepo_await_check(proj, wt, T2=("queued", ["weekly-report"], {"issue": 945, "start_ok": True}))
    _canned(proj, monkeypatch, {"issue view 945": _view(), "issue view 944": _view()})
    proj.out("answer", "Q1", "OK")
    assert proj.gh() == []                                               # 前台命令零网络：对外动作只在 tick 做
    t1 = proj.state()["tickets"]["T1"]
    assert t1["status"] == "done"
    assert t1["closeout"] == {"downstream": {"T2": {"issue": 945, "state": "pending"}},
                              "issue": "pending", "worktree": "pending", "branch": "pending"}
    assert _writes(proj.gh()) == []                                      # 对外动作只在 tick 做
    proj.out("tick")
    log = proj.gh()
    at = lambda pred: next(i for i, c in enumerate(log) if pred(c))
    i1 = at(lambda c: c[:3] == ["issue", "view", "945"] and "state,comments" in c)
    i2 = at(lambda c: c[:3] == ["issue", "comment", "945"])
    i3 = at(lambda c: c[:3] == ["issue", "view", "944"])
    i4 = at(lambda c: c[:3] == ["issue", "comment", "944"])
    i5 = at(lambda c: c == ["issue", "close", "944", "-R", "o/r", "--reason", "completed"])
    assert i1 < i2 < i3 < i4 < i5
    assert "<!-- router:downstream:T1->T2 -->" in log[i2][log[i2].index("--body") + 1]
    assert "<!-- router:closeout:T1 -->" in log[i4][log[i4].index("--body") + 1]
    assert not wt.exists() and not _branch_exists(proj, "router/T1")
    st = proj.state()
    w = st["tickets"]["T1"]["worker"]
    assert w["worktree"] is None and w["branch"] is None
    co = st["tickets"]["T1"]["closeout"]
    assert co["downstream"]["T2"]["state"] == "done" and (co["issue"], co["worktree"], co["branch"]) == ("done",) * 3
    board = (proj.home / "board.html").read_text()
    assert "router-T1" not in board and "claude --resume sid-t1-closeout" not in board
    assert st["tickets"]["T2"]["status"] == "kickoff"                    # 核对完才放锁


OLD_DOWNSTREAM = ("上游 T1 #944「周报口径」已完成（合进 staging），SHA `仓外改动，无 commit`。\n- 合了什么：x\n"
                  "- 对本票的影响：共享代码面 weekly-report 已释放；开工 / 继续前先基于最新 staging。\n\n由 /router 自动留言。")


def _legacy_field_ticket(proj):
    _live_guard(proj)
    git("branch", "router/T1", "origin/staging", cwd=proj.root)
    seed(proj, T1=("merged_staging", ["weekly-report"], {
             "merge_sha": None, "issue": 944, "title": "周报口径", "card_confirmed": True,
             "worker": {"session_id": "sid-legacy", "worktree": str(proj.root / ".claude/worktrees/router-T1"),
                        "branch": "router/T1", "runs": 2}}),
         T3=("queued", ["weekly-report"], {"issue": 946}))


def test_walkthrough_done_recovers_legacy_field_ticket(proj, monkeypatch):   # 复刻现场
    _legacy_field_ticket(proj)
    _canned(proj, monkeypatch, {"issue view 946": _view("OPEN", OLD_DOWNSTREAM), "issue view 944": _view()})
    proj.out("walkthrough-done", "T1")
    proj.out("tick")
    st = proj.state(); log = proj.gh()
    assert st["tickets"]["T1"]["status"] == "done"
    assert [c for c in log if c[:3] == ["issue", "comment", "944"]]
    assert ["issue", "close", "944", "-R", "o/r", "--reason", "completed"] in log
    assert not [c for c in log if c[:3] == ["issue", "comment", "946"]]      # 旧格式留言已在：不重复
    assert not _branch_exists(proj, "router/T1")
    w = st["tickets"]["T1"]["worker"]
    assert w["worktree"] is None and w["branch"] is None
    again = proj.out("walkthrough-done", "T1")                               # 收尾完成后再说一次：幂等
    assert again["ok"] is True and again.get("already") is True


def test_legacy_ticket_downstream_without_old_comment_is_notified(proj, monkeypatch):   # A3 #13
    _legacy_field_ticket(proj)
    _canned(proj, monkeypatch, {"issue view 946": _view(), "issue view 944": _view()})
    proj.out("walkthrough-done", "T1")
    proj.out("tick")
    c946 = [c for c in proj.gh() if c[:3] == ["issue", "comment", "946"]]
    assert len(c946) == 1 and "<!-- router:downstream:T1->T3 -->" in c946[0][c946[0].index("--body") + 1]


def test_answer_handson_reject_resumes_worker_in_executing(proj):
    wt = proj.tmp / "wt"; wt.mkdir()
    _offrepo_await_check(proj, wt)
    proj.out("answer", "Q1", "不满意：口径改回去，别动 releases", "--reject")
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "executing"
    wait_until(lambda: proj.claude())
    argv = proj.claude()[0]
    assert argv[argv.index("--resume") + 1] == "sid-t1-closeout"
    prompt = argv[argv.index("-p") + 1]
    assert "不满意：口径改回去，别动 releases" in prompt and "backups" in prompt
    assert not [c for c in proj.gh() if c[:2] == ["issue", "close"]]
    assert proj.state()["queue"] == []


def test_answer_handson_reject_without_worktree_keeps_item(proj):
    _offrepo_await_check(proj, None)
    r = proj.run("answer", "Q1", "不满意", "--reject", check=False)
    assert r.returncode == 1 and [q["id"] for q in proj.state()["queue"]] == ["Q1"]


def test_answer_handson_inrepo_advances_to_await_prod(proj):
    s = seed(proj, T1=("merged_staging", ["her-reply-prompt"], {"merge_sha": "abc123"}))
    _set_queue(proj, s, _item("Q1", "T1", "handson", summary="staging 走查 T1"))
    r = proj.run("answer", "Q1", "不满意", "--reject", check=False)          # 仓内已合 staging：没有驳回通道
    assert r.returncode == 1 and "跟进票" in json.loads(r.stdout)["error"]
    assert [q["id"] for q in proj.state()["queue"]] == ["Q1"]
    proj.out("answer", "Q1", "走查过了")
    st = proj.state()
    assert st["tickets"]["T1"]["status"] == "await_prod" and st["queue"] == []


def test_closeout_keeps_branch_with_unpushed_commits(proj, monkeypatch):
    wt = _wt_for_t1(proj)
    (wt / "local.txt").write_text("x\n"); git("add", "local.txt", cwd=wt); git("commit", "-qm", "local", cwd=wt)
    _offrepo_await_check(proj, wt)
    _canned(proj, monkeypatch, {"issue view 944": _view()})
    proj.out("answer", "Q1", "OK")
    proj.out("tick")
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "done" and t["closeout"]["branch"].startswith("kept")
    assert t["worker"]["branch"] == "router/T1" and t["worker"]["worktree"] is None
    assert _branch_exists(proj, "router/T1") and not wt.exists()


# ---- R7 + H2：方向审的 codex 读边界（⑨） ---------------------------------------------
#
# 假 codex：按 argv 里的 `-c permissions.router_review.filesystem={"<路径>"="deny|read", …}` 模拟沙箱
# （最长前缀生效；不在任何条目下 = :read-only 默认可读）。exec 读 stdin 里的 `cat <路径>` 照策略「执行」。
#   SHIM_VERSION  --version 的输出                 SHIM_SANDBOX=leak  sandbox 预检读到了内容（边界没生效）
#   SHIM_EXEC     answer（默认，回审查结论）| emulate（按策略执行 cat）| ignore_home（假装拒读 HOME 及罩住 HOME 的根都不存在）
#                 | leak_all | silent | pack_only | model_words | recite（只真跑 pack 那条，其余被拒的「输出」是模型照抄的）
#   SHIM_PROBE    逐个探针改行为，逗号分隔 <探针>=<行为>；探针 pack/deny/sibling/tmp，行为 skip（不执行）
#                 | leak（读出内容）| nofile（报 No such file）| rc0（报 Operation not permitted 但 exit 0）
#   SHIM_PROMPT_DROP  debug prompt-input 的拒读段少列最后一条（预检 1 完整性）
#   SHIM_RC       exec 的退出码（默认 0；124 = 超时）
#   CODEX_LOG     每次调用记一行 {"argv", "cwd", "stdin"}
# exec 带 --json 时照 codex-cli 0.144.4 的 JSONL 事件输出：command_execution（command / aggregated_output /
# exit_code）与 agent_message（text）；不带就输出纯文本。
CODEX_SHIM = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
real = lambda p: os.path.realpath(os.path.expanduser(p))

def record(**kw):
    if os.environ.get("CODEX_LOG"):
        with open(os.environ["CODEX_LOG"], "a") as f:
            f.write(json.dumps({"argv": args, "cwd": os.getcwd(), **kw}, ensure_ascii=False) + "\n")

def profile():
    fs = {}
    for i, a in enumerate(args):
        if a == "-c" and i + 1 < len(args) and ".filesystem=" in args[i + 1]:
            for p, m in re.findall(r'"([^"]+)"\s*=\s*"(\w+)"', args[i + 1].split("=", 1)[1]):
                fs[real(p)] = m
    return fs

def mode_of(fs, path):
    p, best = real(path), None
    for k, m in fs.items():
        if p == k or p.startswith(k.rstrip("/") + "/"):
            if best is None or len(k) > len(best[0]):
                best = (k, m)
    return best[1] if best else None

if args[:1] == ["--version"]:
    print(os.environ.get("SHIM_VERSION", "codex-cli 0.144.4")); sys.exit(0)
if args[:2] == ["debug", "prompt-input"]:
    record()
    print("<permissions>\nThe active permission profile denies reading these paths. Denied filesystem reads:")
    denied = [k for k, m in profile().items() if m == "deny"]
    if os.environ.get("SHIM_PROMPT_DROP"):
        denied = denied[:-1]
    for k in denied:
        print(f'- {k} escalatable="false"')
    sys.exit(0)
if args[:1] == ["sandbox"]:
    record()
    target = args[args.index("--") + 1:]
    if os.environ.get("SHIM_SANDBOX") == "leak":
        print("users.json\nnotes.txt"); sys.exit(0)
    print(f"ls: {target[-1]}: Operation not permitted", file=sys.stderr); sys.exit(1)
if args[:1] == ["exec"]:
    stdin = sys.stdin.read()
    record(stdin=stdin)
    mode = os.environ.get("SHIM_EXEC", "answer")
    if mode == "answer":
        print(json.dumps({"pass": True, "challenges": []})); sys.exit(0)
    cdir = real(args[args.index("-C") + 1]) if "-C" in args else os.getcwd()
    fs = profile()
    if mode == "ignore_home":                                   # 默认拒绝那一层没生效：HOME 和罩住 HOME 的根都不算
        h = real("~")
        fs = {k: v for k, v in fs.items() if not (h == k or h.startswith(k.rstrip("/") + "/"))}
    per = dict(x.split("=", 1) for x in os.environ.get("SHIM_PROBE", "").split(",") if "=" in x)
    def kind(p):
        if "/_canary/pack/" in p: return "pack"
        if "/_canary/deny/" in p: return "deny"
        if "router-canary-probe-" in p: return "tmp"
        if "/.router-canary-probe/" in p: return "sibling"
        return "other"
    as_json = "--json" in args
    events, said = [], []
    def emit(o):
        events.append(o)
    emit({"type": "thread.started", "thread_id": "shim"}); emit({"type": "turn.started"})
    for i, p in enumerate(re.findall(r"cat\s+['\"`]?([^\s'\"`]+)", stdin)):
        inside = real(p).startswith(cdir.rstrip("/") + "/")
        how = per.get(kind(p), "")
        if how == "skip" or mode == "silent" or (mode in ("pack_only", "model_words", "recite") and not inside):
            if mode == "recite" and not inside:
                said.append(f"cat: {p}: Operation not permitted")
            continue
        allowed = mode in ("leak_all", "pack_only", "model_words", "recite") or how == "leak" or mode_of(fs, p) != "deny"
        if how == "nofile":
            out, rc = f"cat: {p}: No such file or directory\n", 1
        elif how == "rc0":
            out, rc = f"cat: {p}: Operation not permitted\n", 0
        elif allowed:
            try:
                out, rc = open(p).read(), 0
            except OSError as e:
                out, rc = f"cat: {p}: {e.strerror}\n", 1
        else:
            out, rc = f"cat: {p}: Operation not permitted\n", 1
        emit({"type": "item.completed", "item": {"id": f"item_{i}", "type": "command_execution",
              "command": f"/bin/zsh -lc 'cat {p}'", "aggregated_output": out, "exit_code": rc,
              "status": "completed" if rc == 0 else "failed"}})
        said.append(out.rstrip("\n"))
    if mode == "model_words":
        said.append("这个路径被策略 denied / Permission denied，我不去读它。")
    emit({"type": "item.completed", "item": {"id": "item_last", "type": "agent_message", "text": "\n".join(said)}})
    emit({"type": "turn.completed"})
    if as_json:
        for o in events:
            print(json.dumps(o, ensure_ascii=False))
    else:
        print("\n".join(said))
    sys.exit(int(os.environ.get("SHIM_RC", "0")))
print("unknown", args, file=sys.stderr); sys.exit(2)
'''


def fs_policy(profile_args):
    """profile_args → {realpath: "deny" | "read"}（与假 codex 同一套解析）。"""
    for a in profile_args:
        if ".filesystem=" in a:
            return {os.path.realpath(os.path.expanduser(p)): m
                    for p, m in re.findall(r'"([^"]+)"\s*=\s*"(\w+)"', a.split("=", 1)[1])}
    raise AssertionError(f"profile_args 里没有 filesystem 表：{profile_args}")


def covering(pol, path):
    """→ (罩住它的最具体条目, 模式)；没有条目罩住 = (None, None)，即 :read-only 默认可读。"""
    p, best = os.path.realpath(os.path.expanduser(str(path))), (None, None)
    for k, m in pol.items():
        if (p == k or p.startswith(k.rstrip("/") + "/")) and (best[0] is None or len(k) > len(best[0])):
            best = (k, m)
    return best


def decide(pol, path):
    return covering(pol, path)[1]


@pytest.fixture
def rv(proj, monkeypatch):
    """HOME = tmp：数据目录、项目、同级目录都在 HOME 下（和现场一样）。codex 换成假 codex。"""
    t = proj.tmp
    monkeypatch.setenv("HOME", str(t))
    (t / "bin" / "codex").write_text(CODEX_SHIM); (t / "bin" / "codex").chmod(0o755)
    monkeypatch.setenv("CODEX_LOG", str(t / "codex.log"))
    for k in ("SHIM_VERSION", "SHIM_SANDBOX", "SHIM_EXEC", "SHIM_PROBE", "SHIM_PROMPT_DROP", "SHIM_RC"):
        monkeypatch.delenv(k, raising=False)
    daily = t / "daily"
    (daily / "runs" / "2026-10-26" / "users.json").write_text(
        json.dumps({"5eed0001": {"quote": "the purple elephant reads poems on mondays"}}))
    (daily / "weekly.py").write_text("WINDOW_DAYS = 7\n")
    (daily / "brief.md").write_text("给 5eed0001 的说明\n")
    (daily / "notes.txt").write_text("plain notes\n")
    (t / "elsewhere").mkdir(); (t / "elsewhere" / "x.py").write_text("x = 1\n")
    for d in (".codex/sessions", ".claude/projects", ".nvm/versions/node/v18/bin"):
        (t / d).mkdir(parents=True, exist_ok=True)
    (t / ".codex" / "config.toml").write_text("model = 'x'\n")
    (proj.home / "surfaces.json").write_text(json.dumps({
        "weekly-report": {"weight": "light", "globs": [f"{daily}/**"]},
        "her-reply-prompt": {"weight": "heavy", "globs": ["crates/persona-engine/src/**"]}}))
    _cfg(proj, leak_guard_globs=[f"{daily}/runs/*/*.json"])
    canary = proj.home / "review" / "canary.json"; canary.parent.mkdir(parents=True)
    canary.write_text(json.dumps({"codex_version": "codex-cli 0.144.4", "ok": True, "reason": "", "at": "2026-11-02T09:00:00Z"}))
    proj.daily = daily
    proj.codex_log = lambda: [json.loads(l) for l in (t / "codex.log").read_text().splitlines()] if (t / "codex.log").exists() else []
    return proj


def _offrepo_card(p, text=None):
    card = p.home / "cards" / "T1.md"; card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text(text or (
        "## 方向卡\n**1. 问题**\n窗口口径\n**3. 方案方向**\n"
        "- 改 ~/daily/weekly.py:43 的窗口\n"
        "- 参考 ~/daily/brief.md\n"
        f"- 数据：{p.daily}/runs/2026-10-26/users.json\n"
        "- 笔记 ~/daily/notes.txt\n"
        f"- 另见 {p.tmp}/elsewhere/x.py：12\n"))
    seed(p, T1=("kickoff", ["weekly-report"], {
        "title": "周报口径", "problem": "窗口口径", "card": {"path": str(card)},
        "why": {"metric": "m", "users": "3 人", "evidence": "~/daily/runs/2026-10-26/summary.json"},
        "worker": {"session_id": "s", "worktree": None}}))
    return card


def _pack_out(p, *extra, check_ok=True):
    r = p.run("review-pack", "T1", *extra, check=False)
    out = json.loads(r.stdout)
    if check_ok:
        assert out["ok"] is True, out
    return out


def test_review_pack_offrepo_copies_only_clean_code(rv):
    _offrepo_card(rv)
    out = _pack_out(rv)
    pack = rv.home / "review" / "T1" / "pack"
    assert out["mode"] == "pack" and os.path.realpath(out["dir"]) == os.path.realpath(pack)
    files = sorted(str(f.relative_to(pack)) for f in pack.rglob("*") if f.is_file())
    assert len(files) == 2 and files[0] == "card.md" and files[1].startswith("src/") and files[1].endswith("daily/weekly.py")
    assert [e["reason"] for e in out["excluded"]] == ["leak", "data_dir", "ext", "outside_surface"]
    script = Path(out["script"])
    text = script.read_text()
    for part in ('default_permissions="router_review"', "--skip-git-repo-check", "timeout 330", "-C"):
        assert part in text, part
    pol = fs_policy(out["profile_args"])
    assert decide(pol, rv.daily / "weekly.py") == "deny" and decide(pol, rv.home / "state.json") == "deny"
    assert decide(pol, pack / "card.md") == "read"
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    rec = [x for x in rv.codex_log() if x["argv"][:1] == ["exec"]][-1]
    assert rec["stdin"] == Path(out["prompt"]).read_text()               # PROMPT 经 stdin 喂入，codex 不用读文件
    assert os.path.realpath(rec["argv"][rec["argv"].index("-C") + 1]) == os.path.realpath(pack)
    assert (rv.home / "cards" / "T1.codex.md").read_text().strip()
    _offrepo_card(rv, "## 方向卡\n**3. 方案方向**\n- 只改窗口常量，不引用文件\n")      # 抽不到路径：照样能审
    out = _pack_out(rv)
    assert sorted(str(f.relative_to(pack)) for f in pack.rglob("*") if f.is_file()) == ["card.md"]   # 先删后建


def test_review_pack_inrepo_writes_prompt_outside_worktree_and_script_runs(rv):
    wt = _wt_for_t1(rv)
    card = rv.home / "cards" / "T1.md"; card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("## 方向卡\n**3. 方案方向**\n- 改 crates/persona-engine/src/prompt_builder.rs 的代词槽\n")
    seed(rv, T1=("kickoff", ["her-reply-prompt"], {"card": {"path": str(card)}, "problem": "代词",
                                                   "worker": {"session_id": "s", "worktree": str(wt), "branch": "router/T1"}}))
    out = _pack_out(rv)
    assert out["mode"] == "worktree" and os.path.realpath(out["dir"]) == os.path.realpath(wt)
    assert os.path.realpath(out["prompt"]) == os.path.realpath(rv.home / "review" / "T1" / "PROMPT.md")
    assert decide(fs_policy(out["profile_args"]), wt / "Cargo.toml") != "deny"
    r = subprocess.run(["bash", out["script"]], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    rec = [x for x in rv.codex_log() if x["argv"][:1] == ["exec"]][-1]
    assert rec["stdin"] == Path(out["prompt"]).read_text()
    assert os.path.realpath(rec["argv"][rec["argv"].index("-C") + 1]) == os.path.realpath(wt)
    assert '"pass"' in (rv.home / "cards" / "T1.codex.md").read_text()
    assert git("status", "--porcelain", cwd=wt) == ""                    # 审查目录里什么都没写


def test_review_pack_refuses_card_with_uid(rv):
    _offrepo_card(rv, "## 方向卡\n**3. 方案方向**\n- 用户 5eed0001 说的那件事\n")
    out = _pack_out(rv, check_ok=False)
    assert out["ok"] is False and "5eed0001" in json.dumps(out.get("hits"))
    assert not (rv.home / "review" / "T1").exists()


def test_review_pack_fails_closed_when_boundary_not_enforced(rv, monkeypatch):    # A3 #14
    _offrepo_card(rv)
    monkeypatch.setenv("SHIM_SANDBOX", "leak")
    out = _pack_out(rv, check_ok=False)
    assert out["ok"] is False and out["reason"] == "boundary"
    assert "codex_cmd" not in out and not (rv.home / "review" / "T1" / "codex.sh").exists()


def test_review_pack_fails_closed_without_codex(rv, monkeypatch):
    _offrepo_card(rv)
    (rv.tmp / "bin" / "codex").unlink()
    monkeypatch.setenv("PATH", ":".join(d for d in os.environ["PATH"].split(":") if d and not (Path(d) / "codex").exists()))
    out = _pack_out(rv, check_ok=False)
    assert out["ok"] is False and out["reason"] == "boundary"
    assert not (rv.home / "review" / "T1" / "codex.sh").exists()


def test_review_pack_requires_fresh_canary_for_codex_version(rv, monkeypatch):
    _offrepo_card(rv)
    monkeypatch.setenv("SHIM_VERSION", "codex-cli 0.145.0")             # codex 自动升级了，金丝雀还是旧版本的
    out = _pack_out(rv, check_ok=False)
    assert out["ok"] is False and out["reason"] == "canary_stale"
    monkeypatch.setenv("SHIM_VERSION", "codex-cli 0.144.4")
    (rv.home / "review" / "canary.json").write_text(json.dumps(
        {"codex_version": "codex-cli 0.144.4", "ok": False, "reason": "canary_leak", "at": "2026-11-02T09:00:00Z"}))
    out = _pack_out(rv, check_ok=False)
    assert out["ok"] is False and out["reason"] == "canary_failed"


@pytest.mark.parametrize("mode,ok,reason", [
    ("emulate", True, None),                          # PACK 令牌 + notes.txt: Operation not permitted，没有 DENY 令牌
    ("leak_all", False, "canary_leak"),               # 出现 DENY 令牌
    ("silent", False, "canary_inconclusive"),         # 什么都没有
    ("pack_only", False, "canary_inconclusive"),      # 只有 PACK 令牌
    ("model_words", False, "canary_inconclusive"),    # PACK + 模型自己写的「denied / Permission denied」，没有 OS 报错
])
def test_review_canary_passes_only_with_os_denial_and_positive_control(rv, monkeypatch, mode, ok, reason):   # A3 #15
    monkeypatch.setenv("SHIM_EXEC", mode)
    r = rv.run("review-canary", check=False)
    out = json.loads(r.stdout)
    c = json.loads((rv.home / "review" / "canary.json").read_text())
    assert out["ok"] is ok and c["ok"] is ok and "reason" in c and c["codex_version"] == "codex-cli 0.144.4"
    if reason:
        assert c["reason"] == reason
    assert (rv.home / "review" / "_canary" / "exec.out").exists()
    if mode == "emulate":                                                     # 令牌每次新造：旧输出不能冒充这一次
        first = (rv.home / "review" / "_canary" / "pack" / "readme.txt").read_text()
        rv.run("review-canary", check=False)
        assert (rv.home / "review" / "_canary" / "pack" / "readme.txt").read_text() != first


# ---- H2：默认拒绝 HOME + 白名单；未列入任何名单的同级目录也读不到 ------------------------

def test_review_profile_default_denies_home_and_allowlists_only_needed_dirs(rv):
    home = os.path.realpath(rv.tmp)
    _offrepo_card(rv)
    pol = fs_policy(_pack_out(rv)["profile_args"])
    pack = rv.home / "review" / "T1" / "pack"
    assert decide(pol, rv.tmp / "unlisted-sibling" / "data.txt") == "deny"   # 没列进任何名单，照样读不到
    assert decide(pol, rv.tmp / "Documents" / "secret.md") == "deny"
    assert decide(pol, pack / "card.md") == "read"
    for sensitive in (rv.daily / "weekly.py", rv.daily / "runs" / "2026-10-26" / "users.json",
                      rv.home / "state.json", rv.tmp / ".codex" / "sessions" / "s.jsonl",
                      rv.tmp / ".claude" / "projects" / "p.jsonl"):
        assert decide(pol, sensitive) == "deny", sensitive
    assert decide(pol, rv.tmp / ".codex" / "config.toml") == "read"            # codex 自己的配置
    assert decide(pol, rv.tmp / ".nvm" / "versions" / "node" / "v18" / "bin" / "rg") == "read"   # 运行时
    for k, m in pol.items():                                                   # 放行项不能罩住 HOME 或敏感目录
        if m == "read":
            for guarded in (home, os.path.realpath(rv.daily), os.path.realpath(rv.home)):
                assert not (guarded == k or guarded.startswith(k.rstrip("/") + "/")), k
    # 仓内票：放行的是 worktree，主 checkout 的其余部分仍被拒
    wt = _wt_for_t1(rv)
    card = rv.home / "cards" / "T1.md"; card.write_text("## 方向卡\n**3. 方案方向**\n- 改 prompt_builder.rs\n")
    seed(rv, T1=("kickoff", ["her-reply-prompt"], {"card": {"path": str(card)},
                                                   "worker": {"session_id": "s", "worktree": str(wt), "branch": "router/T1"}}))
    pol = fs_policy(_pack_out(rv)["profile_args"])
    assert decide(pol, wt / "Cargo.toml") == "read"
    assert decide(pol, rv.root / ".gitignore") == "deny" and decide(pol, rv.home / "state.json") == "deny"


@pytest.mark.parametrize("mode,ok", [("emulate", True), ("ignore_home", False)],
                         ids=["home-deny-enforced", "home-deny-not-enforced"])
def test_review_canary_probes_unlisted_sibling_dir_under_home(rv, monkeypatch, mode, ok):
    monkeypatch.setenv("SHIM_EXEC", mode)
    out = json.loads(rv.run("review-canary", check=False).stdout)
    rec = [x for x in rv.codex_log() if x["argv"][:1] == ["exec"]][-1]
    pol = fs_policy(rec["argv"])
    home = os.path.realpath(rv.tmp)
    probes = re.findall(r"cat\s+['\"`]?([^\s'\"`]+)", rec["stdin"])
    unlisted = [p for p in probes if covering(pol, p)[0] == home]         # 只被「拒读整个 HOME」这一条罩住
    assert unlisted, f"金丝雀没有探一个未列入任何名单的同级目录：{probes}"
    c = json.loads((rv.home / "review" / "canary.json").read_text())
    assert out["ok"] is ok and c["ok"] is ok
    if not ok:                                                              # 同级目录的假数据被读出来 = 泄漏
        assert c["reason"] == "canary_leak"


# ---- FX：终审后修复轮 ------------------------------------------------------------------------

import shlex  # noqa: E402


def _cs(argv):
    """argv 里每个 -c 的值（按顺序）。"""
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-c" and i + 1 < len(argv)]


def _has_slice(argv, part):
    n = len(part)
    return any(argv[i:i + n] == part for i in range(len(argv) - n + 1))


def _no_sandbox_flag(argv):
    return not [a for a in argv if a in ("-s", "--sandbox") or a.startswith("--sandbox=")]


def test_review_exec_has_no_sandbox_flag_and_uses_same_profile_args_as_preflight(rv, monkeypatch):   # F0
    import review
    argv = shlex.split(review.build_exec_cmd("/r", "/p", "/o", review.build_profile_args(["/d"], ["/r"])))
    assert _no_sandbox_flag(argv), argv                    # 显式 -s 会让 codex 不用 default_permissions 选中的 profile
    assert 'approval_policy="never"' in _cs(argv)
    _offrepo_card(rv)
    out = _pack_out(rv)
    pa = out["profile_args"]
    log = rv.codex_log()
    pre1 = [x for x in log if x["argv"][:2] == ["debug", "prompt-input"]][-1]["argv"]
    pre2 = [x for x in log if x["argv"][:1] == ["sandbox"]][-1]["argv"]
    r = subprocess.run(["bash", out["script"]], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    ex = [x for x in rv.codex_log() if x["argv"][:1] == ["exec"]][-1]["argv"]
    assert _no_sandbox_flag(ex), ex
    for a in (pre1, pre2, ex):
        assert _has_slice(a, pa), a
    assert _cs(ex) == _cs(pre1) == _cs(pre2) == _cs(pa)    # exec 的配置 = 两道预检测过的配置（同一份 profile_args）
    monkeypatch.setenv("SHIM_EXEC", "emulate")
    rv.run("review-canary", check=False)
    log = rv.codex_log()
    ex = [x for x in log if x["argv"][:1] == ["exec"]][-1]["argv"]
    assert _no_sandbox_flag(ex) and 'approval_policy="never"' in _cs(ex), ex


TMP_ROOT = os.path.realpath("/tmp")


def _tmp_probes():
    return sorted(Path(TMP_ROOT).glob("router-canary-probe-*"))


def test_review_profile_denies_tmp_tmpdir_and_other_users(rv):                     # F2（RV I-2）
    import tempfile
    _offrepo_card(rv)
    pol = fs_policy(_pack_out(rv)["profile_args"])
    assert decide(pol, "/private/tmp/claude-501/bg-task/output.txt") == "deny"    # Claude Code 后台任务输出
    assert decide(pol, "/tmp/x") == "deny"
    tmpdir = os.environ.get("TMPDIR") or tempfile.gettempdir()
    assert decide(pol, os.path.join(tmpdir, "x.txt")) == "deny"
    for root in ("/var/folders", "/private/var/folders", "/Users", "/Volumes"):
        if os.path.isdir(root):
            assert decide(pol, os.path.join(root, "zz-other-user", "secret.txt")) == "deny", root
    pack = rv.home / "review" / "T1" / "pack"
    assert decide(pol, pack / "card.md") == "read"                                 # 放行项更具体：照样生效
    assert decide(pol, rv.tmp / ".nvm" / "versions" / "node" / "v18" / "bin" / "rg") == "read"
    if os.path.realpath(str(pack)).startswith("/private/var/folders/"):             # macOS：pack 就在 /var/folders 下
        assert covering(pol, pack / "card.md")[0] == os.path.realpath(pack)


def test_review_canary_probes_unlisted_dir_outside_home_under_tmp(rv, monkeypatch):   # F2
    monkeypatch.setenv("SHIM_EXEC", "emulate")
    before = _tmp_probes()
    out = json.loads(rv.run("review-canary", check=False).stdout)
    assert out["ok"] is True, out
    rec = [x for x in rv.codex_log() if x["argv"][:1] == ["exec"]][-1]
    pol = fs_policy(rec["argv"])
    probes = re.findall(r"cat\s+['\"`]?([^\s'\"`]+)", rec["stdin"])
    outside = [p for p in probes if os.path.realpath(p).startswith(TMP_ROOT + "/router-canary-probe-")
               and covering(pol, p) == (TMP_ROOT, "deny")]                         # 只被「拒读 /tmp」这一条罩住
    assert len(outside) == 1, probes
    assert _tmp_probes() == before                                                  # 每次建删，不留探针目录
    monkeypatch.setenv("SHIM_PROBE", "tmp=leak")                                    # /tmp 下的假数据被读出来 = 泄漏
    out = json.loads(rv.run("review-canary", check=False).stdout)
    c = json.loads((rv.home / "review" / "canary.json").read_text())
    assert out["ok"] is False and c["reason"] == "canary_leak" and "tmp" in c["leaked"]
    monkeypatch.setenv("SHIM_PROBE", "tmp=skip")                                    # 没探 /tmp：不算过
    out = json.loads(rv.run("review-canary", check=False).stdout)
    c = json.loads((rv.home / "review" / "canary.json").read_text())
    assert out["ok"] is False and c["ok"] is False and c["reason"] == "canary_inconclusive"
    assert _tmp_probes() == before
