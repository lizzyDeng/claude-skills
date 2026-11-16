"""一张票多个 PR、session 复用、执行中出新卡（router-multipr-fix-plan v2：缺陷 A–F，附录 G / H，收票删 worktree 不再 --force）。

两套夹具：
- mp：真 git（本地裸仓当 origin）、真子进程、真 CLI、严格假 claude（FAKE_CLAUDE_SESSIONS：拒重复 session、resume 校验 cwd），
      假 gh 的 PR 状态由测试写进 prs.json，`gh pr merge` 真往 origin/staging 推一个提交。
- fk：FakeCtx（git / gh 脚本化），只看状态机怎么走。
夹具全是合成文本：假票号、假 uid（u-test-*）、编造的短句。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FIX, SCRIPTS, make_state
from test_router_cli import FAKE, git_project, push_main, staging_commit, wait_until

import events
import launcher
import store
import tick

CLI = SCRIPTS / "router.py"
OLD = "2026-01-01T00:00:00Z"          # 种子里「上一次运行」的起点：真时钟下 progress 事件都在它之后
DEAD_PID = 999999                     # 不存在的 pid：worker 已退出

GH = r'''#!/usr/bin/env python3
# 假 gh。PR 状态在 $GH_PRS：open = {分支: PR 号}，merged = {PR 号: 合并 sha}，checks = {PR 号: success | failure}
import json, os, subprocess, sys, pathlib
log = pathlib.Path(os.environ["GH_LOG"]); args = sys.argv[1:]
with log.open("a") as f: f.write(json.dumps(args, ensure_ascii=False) + "\n")
prs_p = pathlib.Path(os.environ["GH_PRS"])
prs = json.loads(prs_p.read_text()) if prs_p.exists() else {}
for k in ("open", "merged", "checks"): prs.setdefault(k, {})
calls = [json.loads(l) for l in log.read_text().splitlines()]
creates = sum(1 for c in calls if c[:2] == ["issue", "create"])
if args[:2] == ["issue", "list"]: print("[]")
elif args[:2] == ["issue", "create"]: print(f"https://github.com/o/r/issues/{100 + creates}")
elif args[0] == "api" and "--jq" in args: print("9" + args[1].rsplit("/", 1)[-1])
elif args[:2] == ["pr", "list"]:
    head = args[args.index("--head") + 1]
    state = args[args.index("--state") + 1] if "--state" in args else "open"
    n = prs["open"].get(head)
    ok = n is not None and state == "open" and str(n) not in prs["merged"]
    print(json.dumps([{"number": n}] if ok else []))
elif args[:2] == ["pr", "view"] and "title,body" in args:
    print(json.dumps({"title": "合成标题", "body": "合成验收输出"}))
elif args[:2] == ["pr", "view"]:
    pr = args[2]; merged = prs["merged"].get(pr)
    ok = prs["checks"].get(pr, "success") == "success"
    run = {"__typename": "CheckRun", "name": "e2e", "status": "COMPLETED", "conclusion": "SUCCESS" if ok else "FAILURE"}
    print(json.dumps({"state": "MERGED" if merged else "OPEN", "mergedAt": None,
                      "mergeCommit": {"oid": merged} if merged else None, "headRefOid": "head" + pr,
                      "baseRefName": "staging", "mergeable": "MERGEABLE", "statusCheckRollup": [run]}))
elif args[:2] == ["pr", "merge"]:
    pr = args[2]; helper = os.environ["GH_HELPER"]
    g = lambda *a: subprocess.run(["git", "-C", helper, *a], check=True, capture_output=True, text=True).stdout
    g("fetch", "-q", "origin", "staging"); g("checkout", "-q", "-B", "staging", "origin/staging")
    g("commit", "-q", "--allow-empty", "-m", f"合成 PR (#{pr})"); g("push", "-q", "origin", "HEAD:staging")
    prs["merged"][pr] = g("rev-parse", "HEAD").strip()
    prs_p.write_text(json.dumps(prs))
'''


def _git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def mp(tmp_path, monkeypatch):
    tmp = tmp_path.resolve()                      # macOS 的 /var → /private/var：和 router 存的路径（resolve 过）对得上
    root = tmp / "proj"; home = root / ".claude" / "router"
    git_project(tmp, root); home.mkdir(parents=True)
    helper = tmp / "helper"
    subprocess.run(["git", "clone", "-q", str(tmp / "origin.git"), str(helper)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _git("config", k, v, cwd=helper)
    runs = tmp / "daily" / "runs"; (runs / "2026-10-26").mkdir(parents=True)
    (runs / "2026-10-26" / "users.json").write_text(json.dumps({"u-test-0001": {}}))   # 防泄漏闸要有文件可读（假 uid）
    (tmp / "daily" / "state").mkdir()
    bin_ = tmp / "bin"; bin_.mkdir()
    (bin_ / "gh").write_text(GH); (bin_ / "gh").chmod(0o755)
    cfg = {"repo": "o/r", "base_branch": "staging", "worktree_dir": ".claude/worktrees", "map_title": "合成地图",
           "weekly_runs_dir": str(runs), "lark_config": str(tmp / "no-lark.json"),
           "leak_guard_globs": [str(runs / "*" / "users.json")], "claude_projects_dir": str(tmp / "projects"),
           "slots": {"heavy": 3, "light": 2}, "kickoff_budget_min": 30, "stale_min": 45, "disk_limit_gb": 150,
           "worker": {"model": "m", "permission_mode": "bypassPermissions", "allowed_tools": [], "disallowed_tools": [],
                      "claude_bin": [sys.executable, str(FAKE)]}}
    (home / "config.json").write_text(json.dumps(cfg))
    shutil.copy(FIX / "surfaces.json", home / "surfaces.json")
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    for k, v in (("GH_LOG", tmp / "gh.log"), ("GH_PRS", tmp / "prs.json"), ("GH_HELPER", helper),
                 ("FAKE_CLAUDE_ARGV_LOG", tmp / "claude.log"), ("FAKE_CLAUDE_SESSIONS", tmp / "sessions.json")):
        monkeypatch.setenv(k, str(v))
    for k in ("ROUTER_HOME", "ROUTER_TICKET", "FAKE_CLAUDE_HOOK"):
        monkeypatch.delenv(k, raising=False)

    class P:
        pass
    p = P(); p.root, p.home, p.tmp, p.helper = root, home, tmp, helper

    def run(*args, check=True, env=None):
        r = subprocess.run([sys.executable, str(CLI), "--root", str(root), *args], capture_output=True, text=True,
                           timeout=60, env={**os.environ, **(env or {})})
        if check:
            assert r.returncode == 0, r.stdout + r.stderr
        return r
    p.run = run
    p.out = lambda *a, **k: json.loads(run(*a, **k).stdout)
    p.state = lambda: json.loads((home / "state.json").read_text())
    p.save = lambda s: (home / "state.json").write_text(json.dumps(s, ensure_ascii=False))
    p.gh = lambda: [json.loads(l) for l in (tmp / "gh.log").read_text().splitlines()] if (tmp / "gh.log").exists() else []
    p.claude = lambda: ([json.loads(l) for l in (tmp / "claude.log").read_text().splitlines()]
                        if (tmp / "claude.log").exists() else [])

    def put(**tickets):
        s = make_state(**tickets)
        p.save(s)
        return s
    p.put = put

    def prs(**kw):
        f = tmp / "prs.json"
        d = json.loads(f.read_text()) if f.exists() else {}
        for k, v in kw.items():
            d.setdefault(k, {}).update(v)
        f.write_text(json.dumps(d))
        return d
    p.prs = prs

    def register(sid, path):
        f = tmp / "sessions.json"
        d = json.loads(f.read_text()) if f.exists() else {}
        d[sid] = os.path.realpath(path)
        f.write_text(json.dumps(d))
    p.register = register

    def worktree(tid, branch=None, push=True):
        """router 派单时建的那种 worktree：分支从 origin/staging 切，有一个合成提交（push=True 时已推远端）。"""
        branch = branch or f"router/{tid}"
        wt = root / ".claude" / "worktrees" / branch.replace("/", "-", 1)
        _git("fetch", "-q", "origin", "staging", cwd=root)
        _git("worktree", "add", "-q", "-b", branch, str(wt), "origin/staging", cwd=root)
        (wt / f"{tid}.txt").write_text("合成改动一\n")
        _git("add", ".", cwd=wt); _git("commit", "-qm", f"合成提交 {tid}", cwd=wt)
        if push:
            _git("push", "-q", "origin", branch, cwd=wt)
        return wt
    p.worktree = worktree

    def result(tid, n):
        """第 n 次运行的结果对象（等假 claude 退出）。"""
        log = home / "logs" / f"{tid}-{n}.log"
        wait_until(lambda: launcher.read_result(log) and launcher.read_result(log).get("type") == "result")
        return launcher.read_result(log)
    p.result = result
    return p


def worker(sid="sid-1", wt=None, branch="router/T1", runs=1, pid=None, **kw):
    return {"session_id": sid, "worktree": str(wt) if wt else None, "branch": branch, "runs": runs, "pid": pid,
            "started_at": OLD, **kw}


def head(wt):
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt)


# ---- fk：FakeCtx 版巡检（同 test_tick 的 proj）--------------------------------------------------------
T0 = "2026-11-02T10:00:00Z"
MAIN_SHA = "a" * 40


@pytest.fixture
def fk(tmp_path, fake):
    root = tmp_path / "proj"; home = root / ".claude" / "router"; home.mkdir(parents=True)
    (home / "surfaces.json").write_text((FIX / "surfaces.json").read_text())
    runs = tmp_path / "daily" / "runs"; runs.mkdir(parents=True)
    cfg = {"repo": "o/r", "base_branch": "staging", "worktree_dir": ".claude/worktrees", "map_title": "m",
           "weekly_runs_dir": str(runs), "lark_config": str(tmp_path / "lark.json"), "leak_guard_globs": [],
           "slots": {"heavy": 3, "light": 2}, "kickoff_budget_min": 30, "stale_min": 45, "disk_limit_gb": 150,
           "claude_projects_dir": str(tmp_path / "projects"),
           "worker": {"model": "m", "permission_mode": "bypassPermissions", "allowed_tools": [], "disallowed_tools": []}}

    class P:
        pass
    p = P(); p.root, p.home, p.cfg, p.ctx, p.tmp = root, home, cfg, fake, tmp_path

    def put(**tickets):
        s = make_state(**tickets)
        store.save_state(home / "state.json", s)
        return s
    p.put = put
    p.state = lambda: store.load_state(home / "state.json")
    p.run = lambda: tick.run(p.ctx, root, cfg)
    for b in ("staging", "main"):
        fake.script(["git", "-C", str(root), "ls-remote", "--exit-code", "origin", f"refs/heads/{b}"],
                    f"{MAIN_SHA}\trefs/heads/{b}\n")
    return p


def fk_log(p, tid, text='{"type":"result","is_error":false}', n=1):
    f = p.home / "logs" / f"{tid}-{n}.log"; f.parent.mkdir(exist_ok=True); f.write_text(text)
    return str(f)


def progress(home, tid, phase, ts, **extra):
    """worker 报的进度（直接写事件行：和 events.emit_progress 同形状，带 more 之类的新字段）。"""
    f = Path(home) / "events" / f"{tid}.jsonl"; f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts, "ticket": tid, "type": "progress", "phase": phase, **extra},
                            ensure_ascii=False) + "\n")


# =====================================================================================================
# E. 重新派单复用旧 session id
# =====================================================================================================

def test_fake_claude_strict_sessions(tmp_path):                                           # E1
    reg = tmp_path / "sessions.json"
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()

    def fake(cwd, *args):
        r = subprocess.run([sys.executable, str(FAKE), "-p", "合成", *args], cwd=cwd, capture_output=True, text=True,
                           env={**os.environ, "FAKE_CLAUDE_SESSIONS": str(reg)})
        res = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")][-1]
        return r.returncode, res
    rc, res = fake(a, "--session-id", "sid-x")
    assert rc == 0 and not res["is_error"]
    rc, res = fake(b, "--session-id", "sid-x")                                       # 重复 id：拒
    assert rc == 1 and res["is_error"] and "already in use" in res["result"]
    rc, res = fake(b, "--resume", "sid-x")                                           # 换目录 resume：找不到会话
    assert rc == 1 and res["is_error"] and "No conversation found" in res["result"]
    rc, res = fake(a, "--resume", "sid-nope")
    assert rc == 1 and "No conversation found" in res["result"]
    rc, res = fake(a, "--resume", "sid-x")                                           # 同目录 resume：通过
    assert rc == 0 and not res["is_error"]


def test_dispatch_rotates_used_session_id(mp):                                            # E2
    # 手改形态的 HOLD 票（D196 / D253 这类：router 把跑过的票手工置 hold）：有用过的 session、没有 worktree
    mp.put(T1=("hold", ["llm-config"], {"title": "合成老票", "issue": 11,
                                           "worker": worker("sid-old", None, None, runs=2)}))
    mp.register("sid-old", mp.tmp / "somewhere-else")
    out = mp.out("start", "T1")
    assert out["dispatched"] == ["T1"]
    res = mp.result("T1", 3)
    assert not res["is_error"], res["result"]                                        # 严格假 claude 没拒
    argv = mp.claude()[-1]
    sid = argv[argv.index("--session-id") + 1]
    w = mp.state()["tickets"]["T1"]["worker"]
    assert sid != "sid-old" and w["session_id"] == sid and w["prev_sessions"] == ["sid-old"]
    assert mp.state()["tickets"]["T1"]["status"] == "kickoff"


def test_recover_intent_keeps_receipt_session(fk):                                        # E3（回归）
    L = fk.home / "logs" / "T1-1.log"; R = fk.home / "logs" / "T1-1.receipt.json"
    s = fk.put(T1=("queued", ["llm-config"], {"start_ok": True, "worker": {
        "session_id": "sid-r", "worktree": str(fk.root / ".claude/worktrees/router-T1"), "branch": "router/T1",
        "runs": 1, "started_at": OLD}}))
    s["tickets"]["T1"]["dispatch"] = {"phase": "spawning", "kind": "dispatch", "session_id": "sid-r", "log": str(L),
                                      "receipt": str(R), "at": "2026-11-02T09:58:00Z"}
    store.save_state(fk.home / "state.json", s)
    R.parent.mkdir(parents=True, exist_ok=True)
    R.write_text(json.dumps({"session_id": "sid-r", "pid": 10042, "pid_start": "start-10042"}))
    fk.ctx.alive.add(10042)
    fk.run()
    w = fk.state()["tickets"]["T1"]["worker"]
    assert fk.ctx.spawn_attempts == [] and w["session_id"] == "sid-r" and not w.get("prev_sessions")
    assert fk.state()["tickets"]["T1"]["status"] == "kickoff"


# =====================================================================================================
# 顺带：收票删 worktree 用 --force（有未提交改动也删）⇒ 干净才删，有改动进决定单
# =====================================================================================================

def _checkout_items(p, tid="T1"):
    return [q for q in p.state()["queue"] if q.get("ticket") == tid and q.get("topic") == "checkout"]


def test_settle_dirty_worktree_not_force_removed(mp):                                     # S1
    wt = mp.worktree("T1")
    (wt / "notes.txt").write_text("没提交的合成笔记\n")
    mp.put(T1=("verifying", ["llm-config"], {"title": "合成老票", "issue": 11, "pr": 41, "card_confirmed": True,
                                                "worker": worker("sid-1", wt)}))
    mp.prs(merged={"41": "4" * 40})
    mp.out("tick")
    t = mp.state()["tickets"]["T1"]
    assert t["status"] == "merged_staging"
    assert (wt / "notes.txt").exists() and t["worker"]["worktree"] == str(wt)        # 不 --force：改动原样在
    (q,) = _checkout_items(mp)
    assert q["kind"] == "stuck" and str(wt) in q["summary"] and "notes.txt" in q["summary"]
    assert [x["kind"] for x in mp.state()["queue"] if not x.get("topic")] == ["handson"]
    out = mp.out("answer", q["id"], "合成：看过了")                                    # 回答不续跑 worker
    assert out["resumed"] is None and mp.claude() == []
    mp.out("tick")                                                                   # 还没清 ⇒ 再问一次（新项）
    (q2,) = _checkout_items(mp)
    assert q2["id"] != q["id"] and (wt / "notes.txt").exists()
    mp.out("tick")
    assert [x["id"] for x in _checkout_items(mp)] == [q2["id"]]                      # 没回答之前不重复问
    (wt / "notes.txt").unlink()
    mp.out("answer", q2["id"], "好了")
    mp.out("tick")
    w = mp.state()["tickets"]["T1"]["worker"]
    assert not wt.exists() and w["worktree"] is None and w["last_worktree"] == str(wt)
    assert _checkout_items(mp) == [] and mp.claude() == []


# =====================================================================================================
# A. PR 指针过期：第二个 PR 不验，旧 PR 被重复收票
# =====================================================================================================

def _pr_json(state, sha=None, head="abc123"):
    return json.dumps({"state": state, "mergedAt": None, "mergeCommit": {"oid": sha} if sha else None,
                       "headRefOid": head, "baseRefName": "staging", "mergeable": "MERGEABLE",
                       "statusCheckRollup": [{"__typename": "CheckRun", "name": "rust", "status": "COMPLETED",
                                              "conclusion": "SUCCESS"}]})


def _clean_wt(p, name="wt1"):
    wt = p.root / name; wt.mkdir(parents=True, exist_ok=True)
    p.ctx.script(["git", "-C", str(wt), "status", "--porcelain"], "")
    return wt


def _env(p):
    import router
    (p.home / "config.json").write_text(json.dumps(p.cfg))
    return router.Env(p.root, p.ctx)


def _cli(p, *argv):
    import router
    a = router.build_parser().parse_args(["--root", str(p.root), *argv])
    return getattr(router, "cmd_" + a.cmd.replace("-", "_"))(a, _env(p))


def test_single_pr_settle_keeps_pointer_for_attempt(fk):                                  # A1（回归）
    wt1, wt2 = _clean_wt(fk, "wt1"), _clean_wt(fk, "wt2")
    fk.put(T1=("verifying", ["her-reply-prompt"], {"kind": "business", "authorized": True, "card_confirmed": True,
                                                   "card_confirmed_by": "user", "pr": 41, "attempts": [], "walls": [],
                                                   "worker": {"worktree": str(wt1), "branch": "router/T1"}}),
           T2=("verifying", ["llm-config"], {"card_confirmed": True, "pr": 42,
                                                "worker": {"worktree": str(wt2), "branch": "router/T2"}}))
    fk.ctx.script(["gh", "pr", "view", "41"], _pr_json("MERGED", "a41"))
    fk.ctx.script(["gh", "pr", "view", "42"], _pr_json("MERGED", "a42"))
    fk.run()
    s = fk.state()
    assert (s["tickets"]["T1"]["status"], s["tickets"]["T1"]["pr"]) == ("await_prod", 41)    # 收票不动指针
    assert (s["tickets"]["T2"]["status"], s["tickets"]["T2"]["pr"]) == ("merged_staging", 42)
    _cli(fk, "released", "--tickets", "T1", "--what", "合成方向", "--metrics", "m", "--rollback", "关合成开关")
    att = fk.state()["tickets"]["T1"]["attempts"][-1]
    assert (att["pr"], att["merge_sha"]) == (41, "a41")                              # released 照旧把它记进尝试


def test_verify_skips_already_settled_merge(fk):                                          # A2
    # router 手工续跑已收的票（D219 / D542）：worker 在同一分支开了 #42、报 pr_opened；指针还指着已收过的 #41
    fk.put(T1=("verifying", ["llm-config"], {"card_confirmed": True, "pr": 41, "merge_sha": "a41", "issue": 11,
                                                "verifying_at": T0,
                                                "worker": {"worktree": None, "branch": "router/T1"}}))
    fk.ctx.script(["gh", "pr", "view", "41"], _pr_json("MERGED", "a41"))
    fk.ctx.script(["gh", "pr", "view", "42"],
                  lambda cmd: _pr_json("MERGED", "b42") if fk.ctx.calls_with("pr", "merge", "42") else _pr_json("OPEN"))
    fk.ctx.script(["gh", "pr", "list"], json.dumps([{"number": 42}]))
    fk.run()
    t = fk.state()["tickets"]["T1"]
    assert fk.ctx.calls_with("pr", "merge", "42")                                    # 验的是 #42
    assert (t["pr"], t["merge_sha"]) == (42, "b42")
    assert not fk.ctx.calls_with("pr", "merge", "41")


# =====================================================================================================
# B. 合一个 PR 就收票 / C. 下一轮复用 squash 合并过的旧分支（多轮只给老票、维护票）
# =====================================================================================================

def _maint(**kw):
    return {"kind": "maint", "title": "维护 合成周", "issue": 11, "card_confirmed": True, **kw}


def _ls_remote(p):
    return _git("ls-remote", "origin", cwd=p.root)


def _prompt(argv):
    return argv[argv.index("-p") + 1]


def test_progress_more_recorded_business_rejected(mp):                                    # B1
    mp.put(T1=("executing", ["llm-config"], _maint()),
           T2=("executing", ["her-reply-prompt"], {"kind": "business", "authorized": True}))
    out = mp.out("progress", "pr_opened", "--more", "第 2 条：合成条目", "--ticket", "T1")
    assert out["ok"] and out["event"]["more"] == "第 2 条：合成条目"
    assert events.read_all(mp.home, "T1")[-1]["more"] == "第 2 条：合成条目"
    r = mp.run("progress", "pr_opened", "--more", "合成", "--ticket", "T2", check=False)
    assert r.returncode == 2 and "业务票" in json.loads(r.stdout)["error"]
    assert events.read_all(mp.home, "T2") == []
    r = mp.run("progress", "card_ready", "--more", "合成", "--ticket", "T1", check=False)
    assert r.returncode == 2


def test_tick_settle_with_more_switches_and_resumes(mp):                                  # B2
    wt = mp.worktree("T1")
    mp.put(T1=("verifying", ["llm-config"], _maint(pr=41, more="第 2 条：合成条目", worker=worker("sid-1", wt))),
           T2=("queued", ["llm-config"], {"issue": 12, "start_ok": True, "blocked_by": ["T1"]}))
    mp.register("sid-1", wt)
    mp.prs(merged={"41": staging_commit(mp.root, "合成 PR (#41)")})
    mp.out("tick")
    s = mp.state(); t = s["tickets"]["T1"]
    assert t["status"] == "executing" and t["worker"]["worktree"] == str(wt) and wt.is_dir()     # 同一路径
    assert head(wt) == "router/T1-r2" and t["worker"]["branch"] == "router/T1-r2"
    assert _git("rev-parse", "HEAD", cwd=wt) == _git("rev-parse", "origin/staging", cwd=wt)   # 从 origin/staging 切
    assert store.holds_lock(t) and s["tickets"]["T2"]["status"] == "queued"                   # 持锁，下游不派
    assert [q for q in s["queue"] if q["kind"] in ("handson", "prod")] == []
    assert not [c for c in mp.gh() if c[:2] == ["issue", "comment"]]                          # 下游不留言
    assert [r["pr"] for r in t["rounds"]] == [41] and t["pr"] is None and not t.get("more")
    assert not t.get("round_pending")
    assert subprocess.run(["git", "-C", str(mp.root), "rev-parse", "--verify", "--quiet", "refs/heads/router/T1"],
                          capture_output=True).returncode != 0                                # 旧轮本地分支已删
    assert t["rounds"][0]["branch_cleanup"] == "done"
    res = mp.result("T1", 2)
    assert not res["is_error"], res["result"]                                                 # 严格假 claude：同 cwd 续跑
    p = _prompt(mp.claude()[-1])
    assert "--resume" in mp.claude()[-1] and "PR #41" in p and "router/T1-r2" in p and "第 2 条：合成条目" in p


def test_ci_red_repr_without_more_keeps_round(fk):                                        # B3
    wt = _clean_wt(fk)
    fk.put(T1=("executing", ["llm-config"], _maint(worker={"session_id": "sid", "worktree": str(wt), "pid": 123,
                                                              "branch": "router/T1", "runs": 1, "started_at": T0,
                                                              "log": fk_log(fk, "T1")})))
    progress(fk.home, "T1", "pr_opened", "2026-11-02T10:01:00Z", more="第 2 条：合成条目")
    fk.ctx.script(["gh", "pr", "list"], json.dumps([{"number": 41}]))
    fk.ctx.script(["gh", "pr", "view"], (FIX / "gh_pr_failed.json").read_text())
    fk.run()                                                                         # CI 红 ⇒ 叫醒修
    t = fk.state()["tickets"]["T1"]
    assert t["status"] == "executing" and t["more"] == "第 2 条：合成条目" and len(fk.ctx.spawned) == 1
    fk.ctx.alive.clear()
    Path(t["worker"]["log"]).parent.mkdir(exist_ok=True)
    Path(t["worker"]["log"]).write_text('{"type":"result","is_error":false}')
    fk.ctx.advance(5)
    progress(fk.home, "T1", "pr_opened", "2026-11-02T10:06:00Z")                       # 修好再报：不带 --more
    fk.ctx.script(["gh", "pr", "view"], lambda cmd: (FIX / ("gh_pr_merged.json" if fk.ctx.calls_with("pr", "merge")
                                                            else "gh_pr_green.json")).read_text())
    fk.run()
    t = fk.state()["tickets"]["T1"]
    assert fk.ctx.calls_with("pr", "merge", "41")
    assert t["status"] == "executing" and t["worker"]["branch"] == "router/T1-r2"   # 仍走开下一轮
    assert [r["pr"] for r in t["rounds"]] == [41] and [r["more"] for r in t["rounds"]] == ["第 2 条：合成条目"]
    assert [q for q in fk.state()["queue"] if q["kind"] == "handson"] == []
    assert len(fk.ctx.spawned) == 2 and "--resume" in fk.ctx.spawned[-1]["cmd"]


def test_settle_without_more_unchanged(fk):                                               # B4（回归）
    wt = _clean_wt(fk)
    fk.put(T1=("verifying", ["llm-config"], {"issue": 11, "pr": 41, "card_confirmed": True,
                                                "worker": {"worktree": str(wt), "branch": "router/T1"}}),
           T2=("queued", ["llm-config"], {"issue": 12, "start_ok": True, "blocked_by": ["T1"]}))
    fk.ctx.script(["gh", "pr", "view"], lambda cmd: (FIX / ("gh_pr_merged.json" if fk.ctx.calls_with("pr", "merge")
                                                            else "gh_pr_green.json")).read_text())
    res = fk.run()
    s = fk.state(); t = s["tickets"]["T1"]
    assert (t["status"], t["merge_sha"], t["pr"]) == ("merged_staging", "deadbeef", 41)
    assert t["worker"]["worktree"] is None and t["worker"]["branch"] == "router/T1"
    assert [q["kind"] for q in s["queue"]] == ["handson"] and not t.get("rounds")
    assert fk.ctx.calls_with("issue", "comment") and res["dispatched"] == ["T2"]
    assert fk.ctx.spawned[0]["env"]["ROUTER_TICKET"] == "T2"                              # 只派了下游，没续跑 T1


def test_round_impl_done_without_pr_closes_ticket(fk):                                    # B5
    wt = _clean_wt(fk)
    fk.put(T1=("executing", ["llm-config"], _maint(
        rounds=[{"pr": 41, "merge_sha": "a41", "branch": "router/T1", "at": T0, "more": "第 2 条：合成条目"}],
        worker={"session_id": "sid", "worktree": str(wt), "pid": 123, "branch": "router/T1-r2", "runs": 2,
                "started_at": T0, "log": fk_log(fk, "T1")})),
           T2=("queued", ["llm-config"], {"issue": 12, "start_ok": True, "blocked_by": ["T1"]}))
    progress(fk.home, "T1", "impl_done", "2026-11-02T10:01:00Z")                     # 报过 --more，最后发现没有下一项
    fk.run()
    s = fk.state(); t = s["tickets"]["T1"]
    assert (t["status"], t.get("merge_sha")) == ("merged_staging", "a41")             # 今天：当成零代码票进 await_prod
    assert t["worker"]["worktree"] is None
    (q,) = [q for q in s["queue"] if q["kind"] == "handson"]
    assert "#41" in q["summary"] and "Closes" in q["summary"] and "手关" in q["summary"]
    assert fk.ctx.calls_with("issue", "comment")                                     # 下游照常通知


def test_resume_settled_legacy_opens_round_at_same_path(mp):                              # B6
    wt = mp.worktree("T1")
    _git("worktree", "remove", str(wt), cwd=mp.root)                                 # 收票时删掉的
    s = mp.put(T1=("merged_staging", ["llm-config"], {
        "title": "合成老票", "issue": 11, "pr": 41, "merge_sha": "4" * 40, "card_confirmed": True,
        "worker": worker("sid-1", None, last_worktree=str(wt))}))
    s["seq"]["queue"] = 1
    s["queue"].append({"id": "Q1", "ticket": "T1", "kind": "handson", "summary": "staging 走查 T1：合成老票",
                       "created_at": OLD, "notified_at": OLD})
    mp.save(s)
    mp.register("sid-1", wt)
    out = mp.out("tell", "T1", "合成：接着做第 2 条", "--resume")
    t = mp.state()["tickets"]["T1"]
    assert out["resumed"] == "T1" and t["status"] == "executing"
    assert wt.is_dir() and head(wt) == "router/T1-r2" and t["worker"]["worktree"] == str(wt)
    assert _git("rev-parse", "HEAD", cwd=wt) == _git("rev-parse", "origin/staging", cwd=wt)
    assert [(r["pr"], r["merge_sha"]) for r in t["rounds"]] == [(41, "4" * 40)] and t["pr"] is None
    assert [q for q in mp.state()["queue"] if q["kind"] == "handson"] == []          # 走查项撤掉（收票时出汇总）
    res = mp.result("T1", 2)
    assert not res["is_error"], res["result"]
    assert "router/T1-r2" in _prompt(mp.claude()[-1])


def test_resume_refuses_settled_business_and_released_legacy(mp):                         # B7
    wt1, wt2 = mp.worktree("T1"), mp.worktree("T2")
    mp.put(T1=("await_prod", ["her-reply-prompt"], {"kind": "business", "authorized": True, "card_confirmed": True,
                                                    "card_confirmed_by": "user", "pr": 41, "merge_sha": "4" * 40,
                                                    "attempts": [], "worker": worker("sid-1", wt1)}),
           T2=("postlaunch", ["llm-config"], {"title": "合成老票", "pr": 42, "merge_sha": "5" * 40,
                                                  "card_confirmed": True, "worker": worker("sid-2", wt2, "router/T2")}))
    mp.register("sid-1", wt1); mp.register("sid-2", wt2)
    for tid, word in (("T1", "业务票"), ("T2", "已上线")):
        r = mp.run("tell", tid, "合成：再改一点", "--resume", check=False)
        assert r.returncode == 1 and word in json.loads(r.stdout)["error"], r.stdout
    s = mp.state()
    assert (s["tickets"]["T1"]["status"], s["tickets"]["T2"]["status"]) == ("await_prod", "postlaunch")
    assert mp.claude() == []


def test_round_dirty_worktree_stuck_then_answer_switches_first(mp):                       # C1
    wt = mp.worktree("T1")
    (wt / "T1.txt").write_text("改了没提交\n"); (wt / "wip.txt").write_text("合成草稿\n")
    mp.put(T1=("verifying", ["llm-config"], _maint(pr=41, more="第 2 条：合成条目", worker=worker("sid-1", wt))))
    mp.register("sid-1", wt)
    mp.prs(merged={"41": "4" * 40})
    remote = _ls_remote(mp)
    mp.out("tick")
    s = mp.state(); t = s["tickets"]["T1"]
    (q,) = [q for q in s["queue"] if q["kind"] == "stuck"]
    assert t["status"] == "stuck" and str(wt) in q["summary"] and "wip.txt" in q["summary"]
    assert head(wt) == "router/T1" and (wt / "wip.txt").exists() and (wt / "T1.txt").read_text() == "改了没提交\n"
    assert _ls_remote(mp) == remote                                                  # 没推
    assert not t.get("rounds") and t["round_pending"]["pr"] == 41 and t["pr"] == 41
    r = mp.run("answer", q["id"], "合成：好了", check=False)                          # 没清改动就回答：补切还是失败
    assert r.returncode == 1 and "未提交" in json.loads(r.stdout)["error"]
    assert q["id"] in [x["id"] for x in mp.state()["queue"]] and mp.claude() == []
    _git("checkout", "--", ".", cwd=wt); (wt / "wip.txt").unlink()
    out = mp.out("answer", q["id"], "合成：好了")
    t = mp.state()["tickets"]["T1"]
    assert out["resumed"] == "T1" and t["status"] == "executing" and head(wt) == "router/T1-r2"
    assert [r["pr"] for r in t["rounds"]] == [41] and not t.get("round_pending") and t["pr"] is None
    res = mp.result("T1", 2)
    assert not res["is_error"], res["result"]                                        # 同一路径续跑
    assert "router/T1-r2" in _prompt(mp.claude()[-1])


def test_round_fetch_failure_retries_then_stuck(mp):                                      # C2
    origin, off = mp.tmp / "origin.git", mp.tmp / "origin.off"
    wt = mp.worktree("T1")
    mp.put(T1=("verifying", ["llm-config"], _maint(pr=41, more="第 2 条：合成条目", worker=worker("sid-1", wt))))
    mp.register("sid-1", wt)
    mp.prs(merged={"41": "4" * 40})
    origin.rename(off)                                                               # 网断了：fetch 失败
    res = mp.out("tick")
    t = mp.state()["tickets"]["T1"]
    assert any("T1" in e and "切" in e for e in res["errors"])
    assert (t["status"], t["pr"]) == ("verifying", 41) and not t.get("rounds") and t["round_pending"]["pr"] == 41
    assert head(wt) == "router/T1" and not [q for q in mp.state()["queue"] if q["kind"] == "stuck"]
    off.rename(origin)                                                               # 网好了：下一轮重试切成
    mp.out("tick")
    t = mp.state()["tickets"]["T1"]
    assert t["status"] == "executing" and head(wt) == "router/T1-r2" and [r["pr"] for r in t["rounds"]] == [41]
    assert not mp.result("T1", 2)["is_error"]
    # 另一路：连续两次失败 ⇒ stuck
    wt2 = mp.worktree("T2")
    s = mp.state()
    s["tickets"]["T1"]["status"] = "paused"
    s["tickets"]["T2"] = make_state(T2=("verifying", ["her-reply-prompt"], _maint(
        pr=42, more="第 3 条：合成条目", worker=worker("sid-2", wt2, "router/T2"))))["tickets"]["T2"]
    mp.save(s)
    mp.prs(merged={"42": "5" * 40})
    origin.rename(off)
    try:
        mp.out("tick")
        assert mp.state()["tickets"]["T2"]["status"] == "verifying"
        mp.out("tick")
    finally:
        off.rename(origin)
    t2 = mp.state()["tickets"]["T2"]
    assert t2["status"] == "stuck" and t2["round_pending"]["pr"] == 42 and head(wt2) == "router/T2"
    (q,) = [q for q in mp.state()["queue"] if q["kind"] == "stuck" and q["ticket"] == "T2"]
    assert "连续 2 次" in q["summary"]


# =====================================================================================================
# D. 续跑和发版只认「一个 PR」
# =====================================================================================================

def test_tell_resume_confirmed_card_goes_executing_not_kickoff(mp):                       # D1a
    wt = mp.worktree("T1")
    mp.put(T1=("paused", ["llm-config"], {"title": "合成老票", "card_confirmed": True, "kickoff_at": OLD,
                                          "worker": worker("sid-1", wt)}))
    mp.register("sid-1", wt)
    out = mp.out("tell", "T1", "合成：接着做", "--resume")
    t = mp.state()["tickets"]["T1"]
    assert out["resumed"] == "T1" and t["status"] == "executing"                    # 不是 kickoff
    assert t["waiting_for"] != "按 router 转来的意见改方向卡"
    assert not mp.result("T1", 2)["is_error"]


def test_resume_refuses_when_surface_held_by_other(mp):                                   # D1b
    wt = mp.worktree("T1")
    mp.put(T1=("hold", ["llm-config"], {"title": "合成老票", "card_confirmed": True, "worker": worker("sid-1", wt)}),
           T2=("executing", ["llm-config"], {"title": "合成另一张"}))
    mp.register("sid-1", wt)
    r = mp.run("tell", "T1", "合成：接着做", "--resume", check=False)
    out = json.loads(r.stdout)
    assert r.returncode == 1 and "T2" in out["error"] and out["held"] == {"llm-config": "T2"}
    assert mp.state()["tickets"]["T1"]["status"] == "hold" and mp.claude() == []
    assert events.read_all(mp.home, "T1") == [] and not (mp.home / "inbox" / "T1.jsonl").exists()   # 先拒再投递


def _round_maint(status, **kw):
    return (status, ["llm-config"], _maint(worker=worker("sid-9", None, "router/T2-r2"), **kw))


def test_prod_batch_running_round_ticket_needs_user(mp):                                  # D2a
    push_main(mp.root)
    s41 = staging_commit(mp.root, "合成 PR (#41)")
    mp.put(T1=("await_prod", ["her-reply-prompt"], {"title": "合成老票"}),
           T2=_round_maint("executing", rounds=[{"pr": 41, "merge_sha": s41, "branch": "router/T2", "at": OLD}]))
    out = mp.out("prod-batch")
    assert "batch" in out and out["router_ships"] == [] and out["verified"] and out["commits"] == []   # 轮次的提交对得上
    row = {r["ticket"]: r for r in out["batch"]}
    assert "多轮在做" in row["T2"]["state"] and "要放行" in row["T2"]["state"]
    q = out["queue"]
    assert mp.out("answer", q, "放行")["approved"] == ["T1", "T2"]
    out = mp.out("prod-batch")
    assert out["router_ships"] == ["T1"] and out["queue"] is None                     # 放行后不挡，也不进 router_ships
    assert "已放行" in {r["ticket"]: r for r in out["batch"]}["T2"]["state"]


def test_released_rides_running_and_stuck_round_tickets(mp):                              # D2b
    mp.put(T1=("executing", ["llm-config"], _maint(rounds=[{"pr": 41, "merge_sha": "a" * 40, "branch": "router/T1",
                                                            "at": OLD}])),
           T2=("stuck", ["her-reply-prompt"], _maint(pr=42, round_pending={"pr": 42, "merge_sha": "b" * 40,
                                                                            "branch": "router/T2", "at": OLD})))
    out = mp.out("released", "--tickets", "T1", "T2", "--what", "合成发版", "--metrics", "m",
                 "--t0", "2026-11-16T03:00:00Z")
    s = mp.state()
    assert sorted(x["ticket"] for x in out.get("parked", [])) == ["T1", "T2"] and out["postlaunch"] == {}
    assert (s["tickets"]["T1"]["status"], s["tickets"]["T2"]["status"]) == ("executing", "stuck")
    assert not s["tickets"]["T1"].get("postlaunch") and not s["tickets"]["T2"].get("postlaunch")
    assert s["tickets"]["T1"]["rounds"][0]["shipped_at"] == "2026-11-16T03:00:00Z"
    assert s["tickets"]["T2"]["round_pending"]["shipped_at"] == "2026-11-16T03:00:00Z"
    assert not store.rounds_unshipped(s["tickets"]["T1"]) and not store.rounds_unshipped(s["tickets"]["T2"])


def test_round_ticket_not_pending_after_release(mp):                                      # D2c（反例）
    push_main(mp.root)
    s41, s42 = staging_commit(mp.root, "合成 PR (#41)"), staging_commit(mp.root, "合成 PR (#42)")
    mp.put(T1=("merged_staging", ["llm-config"], _maint(
               pr=42, merge_sha=s42, history=[{"at": OLD, "status": "merged_staging"}],
               rounds=[{"pr": 41, "merge_sha": s41, "branch": "router/T1", "at": OLD}])),
           T2=("await_prod", ["her-reply-prompt"], {"kind": "business", "authorized": True, "card_confirmed": True,
                                                    "card_confirmed_by": "user", "attempts": [], "title": "合成业务票"}))
    out = mp.out("prod-batch")
    assert [r["ticket"] for r in out["batch"]] == ["T1", "T2"]
    mp.out("walkthrough-done", "T1")
    mp.out("released", "--tickets", "T1", "--what", "合成发版", "--metrics", "m")
    t1 = mp.state()["tickets"]["T1"]
    assert t1["status"] == "postlaunch" and not store.staging_pending(t1)
    assert [r["ticket"] for r in mp.out("prod-batch")["batch"]] == ["T2"]           # 发版后不再待发


def test_attribute_counts_all_round_commits():                                            # D3
    import router
    st = make_state(T1=("executing", ["llm-config"], {"kind": "maint", "pr": 43, "merge_sha": "c3",
                                                      "rounds": [{"pr": 41, "merge_sha": "c1"}, {"pr": 42}],
                                                      "round_pending": {"pr": 44, "merge_sha": "c4"}}))
    commits = [{"sha": "c1", "parents": ["p", "q"], "subject": "Merge 合成"},          # merge 提交只认 sha
               {"sha": "x2", "parents": ["p"], "subject": "合成 (#42)"},                # squash：标题带轮次 PR 号
               {"sha": "c3", "parents": ["p"], "subject": "合成"},
               {"sha": "c4", "parents": ["p"], "subject": "合成"},                       # 还没归档的那一轮
               {"sha": "zz", "parents": ["p"], "subject": "别的 (#99)"}]
    assert [c["sha"] for c in router._attribute(st, ["T1"], commits)] == ["zz"]
    t = st["tickets"]["T1"]
    t["merge_sha"] = None                                                            # 还没收票：只有轮次合过
    assert store.staging_pending(t)


# =====================================================================================================
# 端到端：一张维护票两轮 PR（第 1 个带 --more、第 2 个不带），真 git、真子进程、严格假 claude、假 gh
# =====================================================================================================

HOOK = r'''
import json, os, subprocess, sys
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
t, home, cli = os.environ["ROUTER_TICKET"], os.environ["ROUTER_HOME"], os.environ["ROUTER_CLI"]
call = lambda *a: subprocess.run([sys.executable, cli, *a], check=True, capture_output=True)
git = lambda *a: subprocess.run(["git", *a], check=True, capture_output=True, text=True).stdout.strip()
branch = git("rev-parse", "--abbrev-ref", "HEAD")
with open(os.environ["HOOK_LOG"], "a") as f:
    f.write(json.dumps({"cwd": os.path.realpath(os.getcwd()), "branch": branch}) + "\n")

def open_pr(more=None):
    with open(branch.replace("/", "-") + ".txt", "w") as fh:
        fh.write("合成改动\n")
    git("add", "."); git("commit", "-qm", "合成：" + branch); git("push", "-q", "-u", "origin", branch)
    p = os.environ["GH_PRS"]
    prs = json.loads(open(p).read()) if os.path.exists(p) else {}
    prs.setdefault("open", {})[branch] = 41 + len(prs.get("open", {}))
    open(p, "w").write(json.dumps(prs))
    call("progress", "pr_opened", *(["--more", more] if more else []))

if "--session-id" in argv:                                   # 第 1 段：写方向卡
    os.makedirs(home + "/cards", exist_ok=True)
    open(home + "/cards/" + t + ".md", "w").write(
        "## 方向卡\n**3. 方案方向**\n- 合成：两个条目，一条一个 PR\n**4. 为什么不选另一种做法**\n合成\n")
    call("progress", "card_ready")
elif "方向卡已确认" in prompt:                                 # 第 1 条：开 PR，后面还有
    open_pr(more="第 2 条：合成条目")
elif "已合进" in prompt:                                       # 下一轮（router 切好分支叫醒）：最后一条，不带 --more
    open_pr()
'''


def test_e2e_maint_two_rounds_then_settle(mp, monkeypatch):
    import router
    from ctx import Ctx
    monkeypatch.setenv("FAKE_CLAUDE_HOOK", str(mp.tmp / "hook.py"))
    monkeypatch.setenv("HOOK_LOG", str(mp.tmp / "hook.log"))
    (mp.tmp / "hook.py").write_text(HOOK)
    push_main(mp.root)
    mp.put(T1=("draft", ["llm-config"], {"kind": "maint", "title": "维护 合成周",
                                          "problem": "合成：两个条目"}))
    ticks = []

    def tick_until(cond, timeout=30):
        def once():
            ticks.append(mp.out("tick"))
            return cond(mp.state()["tickets"]["T1"], ticks[-1])
        wait_until(once, timeout)

    assert mp.out("start", "T1")["dispatched"] == ["T1"]
    tick_until(lambda t, out: "T1" in out["cards_for_review"])
    q = mp.out("submit-card", "T1", "--card", str(mp.home / "cards" / "T1.md"))["queue"]
    assert mp.out("answer", q, "合成：同意")["resumed"] == "T1"
    tick_until(lambda t, out: t["status"] == "merged_staging")
    t = mp.state()["tickets"]["T1"]
    prs = json.loads((mp.tmp / "prs.json").read_text())
    hook = [json.loads(l) for l in (mp.tmp / "hook.log").read_text().splitlines()]
    # 中途没有「没有可续跑」，也没有 already in use / No conversation found
    errs = " ".join(e for out in ticks for e in out["errors"])
    assert "没有可续跑" not in errs and "already in use" not in errs and "No conversation found" not in errs
    results = [launcher.read_result(f) for f in sorted((mp.home / "logs").glob("T1-*.log"))]
    assert len(results) == 3 and not any(r["is_error"] for r in results), results
    # 第 2 轮分支 router/T1-r2，worktree 路径不变
    assert [h["branch"] for h in hook] == ["router/T1", "router/T1", "router/T1-r2"]
    assert len({h["cwd"] for h in hook}) == 1
    # 只出 1 个走查项；rounds 1 条（第 2 轮是收票那一轮，记在 t.pr / t.merge_sha）
    hs = [q for q in mp.state()["queue"] if q["kind"] == "handson"]
    assert len(hs) == 1 and "#41" in hs[0]["summary"] and "#42" in hs[0]["summary"]
    assert [r["pr"] for r in t["rounds"]] == [41] and t["rounds"][0]["merge_sha"] == prs["merged"]["41"]
    assert (t["pr"], t["merge_sha"]) == (42, prs["merged"]["42"])
    assert t["worker"]["worktree"] is None and not Path(hook[0]["cwd"]).exists()     # 收票：干净才删
    # 两个合并提交在对账里都对得上
    env = router.Env(mp.root, Ctx())
    check = router._staging_commits(env)
    assert check["ok"] and {c["sha"] for c in check["commits"]} >= {prs["merged"]["41"], prs["merged"]["42"]}
    assert router._attribute(mp.state(), ["T1"], check["commits"]) == []
    # released 之后不再待发
    mp.out("walkthrough-done", "T1")
    mp.out("released", "--tickets", "T1", "--what", "合成两条", "--metrics", "m")
    assert not store.staging_pending(mp.state()["tickets"]["T1"])


# =====================================================================================================
# F. 执行中出的新方向卡被当成卡住（D571）
# =====================================================================================================

def test_card_ready_while_executing_goes_to_review(mp):                                   # F1
    wt = mp.worktree("T1")
    log = mp.home / "logs" / "T1-1.log"; log.parent.mkdir(parents=True)
    log.write_text('{"type": "result", "is_error": false, "result": "ok"}')
    mp.put(T1=("executing", ["llm-config"], {
        "title": "合成老票", "card_confirmed": True, "card_confirmed_by": "user",
        "solution": {"kind": "card", "text": "合成：先诊断", "source": "方向卡 v1（已确认）"},
        "worker": worker("sid-1", wt, pid=DEAD_PID, log=str(log))}))
    mp.register("sid-1", wt)
    card = mp.home / "cards" / "T1.md"; card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("## 方向卡\n**3. 方案方向**\n- 合成：拆下一道墙\n**4. 为什么不选另一种做法**\n合成\n")
    mp.out("progress", "card_ready", "--ticket", "T1")                            # 照用户回答写好了新方向卡
    out = mp.out("tick")
    t = mp.state()["tickets"]["T1"]
    assert out["cards_for_review"] == ["T1"] and t["status"] == "kickoff"
    assert t["card_confirmed"] is False and "card_confirmed_by" not in t and "新方向卡待审" in t["solution"]["source"]
    assert not [q for q in mp.state()["queue"] if q["kind"] == "stuck"] and mp.claude() == []   # 不是卡住、没起进程
    assert mp.out("tick")["cards_for_review"] == ["T1"]                           # 重启后 _resurface 能再交一次
    q = mp.out("submit-card", "T1", "--card", str(card))["queue"]
    assert mp.state()["tickets"]["T1"]["status"] == "await_card"
    out = mp.out("answer", q, "合成：同意")
    t = mp.state()["tickets"]["T1"]
    assert out["resumed"] == "T1" and t["status"] == "executing" and t["card_confirmed"] is True
    assert not mp.result("T1", 2)["is_error"]                                     # 同一 session、同一 cwd
    argv = mp.claude()[-1]
    assert argv[argv.index("--resume") + 1] == "sid-1"


def test_new_card_then_pr_same_run_not_merged(fk):                                        # F2
    fk.put(T1=("executing", ["llm-config"], {"card_confirmed": True, "card_confirmed_by": "user", "issue": 11,
                                             "worker": {"session_id": "sid", "worktree": "/wt/x", "pid": 123,
                                                        "branch": "router/T1", "runs": 1, "started_at": T0,
                                                        "log": fk_log(fk, "T1")}}))
    progress(fk.home, "T1", "card_ready", "2026-11-02T10:01:00Z")
    progress(fk.home, "T1", "pr_opened", "2026-11-02T10:02:00Z")                 # 同一次运行：新卡之后又开了 PR
    fk.ctx.script(["gh", "pr", "list"], json.dumps([{"number": 41}]))
    fk.ctx.script(["gh", "pr", "view"], (FIX / "gh_pr_green.json").read_text())
    res = fk.run()
    t = fk.state()["tickets"]["T1"]
    assert (t["status"], t["card_confirmed"]) == ("kickoff", False) and res["cards_for_review"] == ["T1"]
    assert not fk.ctx.calls_with("pr", "merge")                                   # 新卡没确认：PR 不合


def test_previous_run_events_in_same_second_not_reread(fk):                               # F3（事件边界）
    # 时间戳只到秒：上一次运行的 progress 和这次续跑的 started_at 落在同一秒时，「started_at 之后」会把旧事件也算进来：
    # 旧的 card_ready 被当成执行中出的新卡；第 1 轮的 pr_opened --more 被第 2 轮再读一次 ⇒ 最后一个 PR 被当成「后面还有」
    wt = _clean_wt(fk)
    card = fk.home / "cards" / "T1.md"; card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("## 方向卡\n**3. 方案方向**\n- 合成：两个条目\n**4. 为什么不选另一种做法**\n合成\n")
    fk.put(T1=("kickoff", ["llm-config"], _maint(card_confirmed=False, worker={
        "session_id": "sid", "worktree": str(wt), "pid": 123, "branch": "router/T1", "runs": 1, "started_at": T0,
        "log": fk_log(fk, "T1")})))
    fk.ctx.script(["gh", "pr", "list"], lambda cmd: json.dumps([{"number": 42 if "router/T1-r2" in cmd else 41}]))
    merged = lambda pr: fk.ctx.calls_with("pr", "merge", pr)
    fk.ctx.script(["gh", "pr", "view", "41"], lambda cmd: _pr_json("MERGED", "a41") if merged("41") else _pr_json("OPEN"))
    fk.ctx.script(["gh", "pr", "view", "42"], lambda cmd: _pr_json("MERGED", "b42") if merged("42") else _pr_json("OPEN"))

    def worker_exits(*evs):                                                          # 这次运行报的进度（都在同一秒 T0）
        t = fk.state()["tickets"]["T1"]
        fk.ctx.alive.clear()
        Path(t["worker"]["log"]).parent.mkdir(exist_ok=True)
        Path(t["worker"]["log"]).write_text('{"type":"result","is_error":false}')
        for phase, extra in evs:
            progress(fk.home, "T1", phase, T0, **extra)
        return fk.run()
    assert worker_exits(("card_ready", {}))["cards_for_review"] == ["T1"]
    q = _cli(fk, "submit-card", "T1", "--card", str(card))["queue"]
    assert _cli(fk, "answer", q, "合成：同意")["resumed"] == "T1"                     # 续跑：started_at 还是 T0
    worker_exits(("pr_opened", {"more": "第 2 条：合成条目"}))                          # 第 1 轮：合 #41 → 开第 2 轮
    t = fk.state()["tickets"]["T1"]
    assert (t["status"], t["worker"]["branch"], t["card_confirmed"]) == ("executing", "router/T1-r2", True)
    worker_exits(("pr_opened", {}))                                                  # 第 2 轮：最后一个 PR
    t = fk.state()["tickets"]["T1"]
    assert (t["status"], t["merge_sha"], t.get("more")) == ("merged_staging", "b42", None)   # 收票，不是第 3 轮
    assert [r["pr"] for r in t["rounds"]] == [41]


# =====================================================================================================
# G. 零代码票按决定收票后，状态仍停在 await_prod，被拉进下一个发版批次（D538 / D541 / D543 → Q445）
# =====================================================================================================

def test_close_zero_code_legacy_leaves_release_batch(mp):                                 # G
    mp.put(T1=("await_prod", ["llm-config"], {"title": "合成零代码票", "issue": 11, "card_confirmed": True,
                                              "waiting_for": tick.ZERO_CODE_WAIT}),
           T2=("await_prod", ["her-reply-prompt"], {"title": "合成老票", "issue": 12}),
           T3=("merged_staging", ["her-reply-prompt"], {"title": "合成在途", "issue": 13, "pr": 43,
                                                        "merge_sha": "c" * 40,
                                                        "history": [{"at": OLD, "status": "merged_staging"}]}))
    assert mp.out("prod-batch")["tickets"] == ["T1", "T2"]
    out = mp.out("close", "T1", "--reason", "合成：前提已失效")
    t = mp.state()["tickets"]["T1"]
    assert out["ok"] and t["status"] == "done" and t["closeout"]["issue"] == "pending"
    assert mp.out("prod-batch")["tickets"] == ["T2"]                                 # 不再被拉进发版批次
    dec = (mp.home / "decisions.md").read_text()
    assert "| T1 | 收票" in dec and "合成：前提已失效" in dec
    r = mp.run("close", "T3", "--reason", "合成", check=False)                       # 改动还在 staging 上：不能直接收
    assert r.returncode == 1 and "staging" in json.loads(r.stdout)["error"]
    r = mp.run("close", "T2", check=False)                                           # 老票收票要写理由
    assert r.returncode == 1 and "--reason" in json.loads(r.stdout)["error"]
    s = mp.state()
    assert (s["tickets"]["T2"]["status"], s["tickets"]["T3"]["status"]) == ("await_prod", "merged_staging")
    mp.out("tick")                                                                   # 收尾：留言说明后关 issue
    assert mp.state()["tickets"]["T1"]["closeout"]["issue"] == "done"
    body = next(c for c in mp.gh() if c[:3] == ["issue", "comment", "11"])[-1]
    assert "收票" in body and "合成：前提已失效" in body and "仓外改动" not in body
    assert ["issue", "close", "11", "-R", "o/r", "--reason", "completed"] in mp.gh()
