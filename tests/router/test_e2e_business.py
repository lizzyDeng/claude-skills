"""业务票全周期端到端（假 claude + 假 gh，真 git、真子进程、真 CLI）：
建票 → 派发 → 方向卡自动确认 → PR 合 staging → await_prod（唤醒 prod_ready）→ router 上 prod → released
→ daily 判 useful 拆墙 → 墙拆完 → 挂新墙 → 提示下一个方向 → redirect → 第二次尝试用新分支和新会话。
夹具全是合成文本，不含真实 uid 和原话。"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest
from conftest import FIX, SCRIPTS
from test_router_cli import FAKE, git_project, push_main

CLI = SCRIPTS / "router.py"

HOOK = r'''
import os, subprocess, sys
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
t, home = os.environ["ROUTER_TICKET"], os.environ.get("ROUTER_HOME")
cli = os.environ.get("ROUTER_CLI")
call = lambda *a: subprocess.run([sys.executable, cli, *a], check=True, capture_output=True)
if t == "analyst":                                           # 分析 agent：产出由 fake_claude 按 OUT_JSON 写
    pass
elif "--session-id" in argv:                                 # 第 1 段：写方向卡
    os.makedirs(f"{home}/cards", exist_ok=True)
    open(f"{home}/cards/{t}.md", "w").write(
        "## 方向卡\n**1. 问题**\nx\n**3. 方案方向**\n- 合成方向：对准第一道墙\n**4. 为什么不选另一种做法**\ny\n")
    call("progress", "card_ready")
elif "方向卡已确认" in prompt:                                 # 第 2、3 段：干完开 PR
    call("progress", "pr_opened")
'''

GH = r'''#!/usr/bin/env python3
import json, os, re, sys, pathlib
log = pathlib.Path(os.environ["GH_LOG"]); args = sys.argv[1:]
with log.open("a") as f: f.write(json.dumps(args, ensure_ascii=False) + "\n")
calls = [json.loads(l) for l in log.read_text().splitlines()]
creates = sum(1 for c in calls if c[:2] == ["issue", "create"])
merged = lambda pr: any(c[:3] == ["pr", "merge", pr] for c in calls)
run = {"__typename": "CheckRun", "name": "e2e", "status": "COMPLETED", "conclusion": "SUCCESS"}
if args[:2] in (["issue", "list"],): print("[]")
elif args[:2] == ["issue", "create"]: print(f"https://github.com/o/r/issues/{100 + creates}")
elif args[0] == "api" and "--jq" in args: print("9" + args[1].rsplit("/", 1)[-1])
elif args[:2] == ["pr", "list"]:
    head = args[args.index("--head") + 1]
    a = re.search(r"-a(\d+)$", head)
    print(json.dumps([{"number": 40 + int(re.search(r"T(\d+)", head).group(1)) + (20 * int(a.group(1)) if a else 0)}]))
elif args[:2] == ["pr", "view"]:
    pr = args[2]
    print(json.dumps({"state": "MERGED" if merged(pr) else "OPEN", "mergedAt": None,
                      "mergeCommit": {"oid": "feedface" + pr} if merged(pr) else None,
                      "headRefOid": "head" + pr, "baseRefName": "staging", "mergeable": "MERGEABLE",
                      "statusCheckRollup": [run], "title": "t", "body": "验收输出 Refs #101"}))
'''


@pytest.fixture
def e2e(tmp_path, monkeypatch):
    root = tmp_path / "proj"; home = root / ".claude" / "router"
    git_project(tmp_path, root); push_main(root); home.mkdir(parents=True)
    runs = tmp_path / "daily" / "runs"; runs.mkdir(parents=True)          # 没有已发出的周报：不起 weekly
    (tmp_path / "daily" / "state").mkdir()
    bin_ = tmp_path / "bin"; bin_.mkdir(); (bin_ / "gh").write_text(GH); (bin_ / "gh").chmod(0o755)
    (tmp_path / "hook.py").write_text(HOOK)
    (tmp_path / "brief.md").write_text("合成 brief\n")
    cfg = {"repo": "o/r", "base_branch": "staging", "worktree_dir": ".claude/worktrees", "map_title": "m",
           "weekly_runs_dir": str(runs), "lark_config": str(tmp_path / "none.json"), "leak_guard_globs": [],
           "slots": {"heavy": 3, "light": 2}, "kickoff_budget_min": 30, "stale_min": 45, "disk_limit_gb": 150,
           "claude_projects_dir": str(tmp_path / "projects"),
           "analyst": {"brief": str(tmp_path / "brief.md"), "daily_at_utc": "23:59"},     # daily 只手动起
           "worker": {"model": "m", "permission_mode": "bypassPermissions", "allowed_tools": [], "disallowed_tools": [],
                      "claude_bin": [sys.executable, str(FAKE)]}}
    (home / "config.json").write_text(json.dumps(cfg))
    shutil.copy(FIX / "surfaces.json", home / "surfaces.json")
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("FAKE_CLAUDE_HOOK", str(tmp_path / "hook.py"))
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_LOG", str(tmp_path / "claude.log"))
    for k in ("ROUTER_HOME", "ROUTER_TICKET", "OUT_JSON", "CARGO_TARGET_DIR"):
        monkeypatch.delenv(k, raising=False)

    class P:
        pass
    p = P(); p.root, p.home, p.tmp = root, home, tmp_path

    def out(*a):
        r = subprocess.run([sys.executable, str(CLI), "--root", str(root), *a], capture_output=True, text=True, timeout=60)
        return json.loads(r.stdout)
    p.out = out
    p.state = lambda: json.loads((home / "state.json").read_text())
    p.claude = lambda: [json.loads(l) for l in (tmp_path / "claude.log").read_text().splitlines()] \
        if (tmp_path / "claude.log").exists() else []
    return p


def until(p, cond, timeout=20):
    """一轮一轮跑 tick，直到 cond(这一轮的输出) 为真；返回那一轮的输出。"""
    end = time.time() + timeout
    while time.time() < end:
        res = p.out("tick")
        if cond(res):
            return res
        time.sleep(0.2)
    raise AssertionError("等不到")


def daily(p, monkeypatch, data):
    monkeypatch.setenv("OUT_JSON", json.dumps(data, ensure_ascii=False))
    rid = p.out("analyst", "run", "--mode", "daily")["run"]
    monkeypatch.delenv("OUT_JSON")
    until(p, lambda res: p.state()["analyst"]["runs"][rid].get("ingested"))
    return rid


def test_business_ticket_full_cycle(e2e, monkeypatch):
    p = e2e
    tid = p.out("add", "--business", "--title", "合成业务票", "--segment", "合成人群", "--want", "合成想要",
                "--wall", "合成墙甲", "--metric", "合成指标", "--surfaces", "llm-config")["ticket"]
    s = p.state(); s.setdefault("analyst", {})["last_daily"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (p.home / "state.json").write_text(json.dumps(s, ensure_ascii=False))

    # 建票 → 派发：第一次尝试在 router/T1，新会话
    assert p.out("start", tid)["dispatched"] == [tid]
    first = p.state()["tickets"][tid]["worker"]
    assert first["branch"] == f"router/{tid}"

    # 方向卡 → 方向审 → submit-card 自动确认（票级授权），worker 续跑开 PR
    until(p, lambda res: res["cards_for_review"] == [tid])
    sub = p.out("submit-card", tid, "--card", str(p.home / "cards" / f"{tid}.md"), "--rounds", "1")
    assert sub["auto_confirmed"] is True and sub["queue"] is None

    # CI 全绿 → 合 staging → await_prod，唤醒 router 去上 prod（#1）
    res = until(p, lambda res: res.get("prod_ready"))
    t = p.state()["tickets"][tid]
    assert res["prod_ready"] == [{"ticket": tid}] and t["status"] == "await_prod" and t["merge_sha"] == "feedface41"
    assert not [q for q in p.state()["queue"] if q["kind"] == "handson"]       # 业务票不走查

    # router 上 prod：批次里只有它，router 自己上（#6），记一次尝试
    pb = p.out("prod-batch")
    assert pb["router_ships"] == [tid] and pb["queue"] is None
    rel = p.out("released", "--tickets", tid, "--what", "合成方向一", "--metrics", "m1", "--rollback", "合成：关开关甲")
    assert rel["ok"]
    t = p.state()["tickets"][tid]
    assert t["status"] == "postlaunch" and t["attempts"][0]["branch"] == f"router/{tid}" and t["attempts"][0]["pr"] == 41

    # daily 判 useful：拆掉唯一一道墙 ⇒ 墙拆完，等提议结束（不自动再派）
    daily(p, monkeypatch, {"verdicts": [{"ticket": tid, "attempt": 1, "verdict": "useful", "touched": 5, "hit_wall": 3,
                                         "continued": 3, "note": "合成：3 人走到墙，墙没了"}], "walls": []})
    t = p.state()["tickets"][tid]
    assert t["attempts"][0]["verdict"] == "useful" and [w["status"] for w in t["walls"]] == ["broken"]
    assert t["status"] == "postlaunch" and t["waiting_for"] == "墙已全部拆完，等分析 agent 提议结束"

    # 挂新墙 ⇒ 决定单提示「要下一个方向吗」（#3）
    daily(p, monkeypatch, {"verdicts": [], "walls": [{"ticket": tid, "text": "合成新墙乙"}]})
    (q,) = [q for q in p.state()["queue"] if q.get("topic") == "next"]
    assert q["ticket"] == tid and "下一个方向" in q["summary"]

    # router 定方向 ⇒ redirect ⇒ 第二次尝试：新会话、新分支、新 worktree
    red = p.out("redirect", tid, "合成方向二", "--as-router")
    assert red["ok"] and q["id"] in red["cleared"]
    res = until(p, lambda res: res["dispatched"] == [tid])
    w = p.state()["tickets"][tid]["worker"]
    assert w["branch"] == f"router/{tid}-a2" and w["worktree"].endswith(f"router-{tid}-a2")
    assert w["session_id"] and w["session_id"] != first["session_id"]
    spawned = lambda: [a for a in p.claude() if "--session-id" in a and a[a.index("--session-id") + 1] == w["session_id"]]
    end = time.time() + 20
    while not spawned() and time.time() < end:                                 # 假 claude 起来才写 argv
        time.sleep(0.1)
    (argv,) = spawned()
    assert "--resume" not in argv
    prompt = argv[argv.index("-p") + 1]
    assert f"router/{tid}-a2" in prompt and "合成新墙乙" in prompt and "已试" in prompt      # #8：新墙 + 已试方向
    head = subprocess.run(["git", "-C", w["worktree"], "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert head == f"router/{tid}-a2"
