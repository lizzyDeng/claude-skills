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
