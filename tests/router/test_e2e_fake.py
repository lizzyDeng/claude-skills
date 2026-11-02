"""端到端（假 claude + 假 gh，真 git、真子进程、真 CLI）：
draft → start --all → worker 写方向卡 → tick 交方向审 → submit-card → answer → worker 开 PR
→ CI 全绿 → tick 合 staging、放锁、给下游留言、自动派下游票 → 看板里有这张票。"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import FIX, SCRIPTS
from test_router_cli import FAKE, git_project

CLI = SCRIPTS / "router.py"

HOOK = r'''
import os, subprocess, sys
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
t, home, cli = os.environ["ROUTER_TICKET"], os.environ["ROUTER_HOME"], os.environ["ROUTER_CLI"]
call = lambda *a: subprocess.run([sys.executable, cli, *a], check=True, capture_output=True)
if "--session-id" in argv:                                   # 第 1 段：写方向卡
    os.makedirs(f"{home}/cards", exist_ok=True)
    open(f"{home}/cards/{t}.md", "w").write(
        "## 方向卡\n**1. 问题**\nx\n**3. 方案方向**\n- 假方向：拆 tells\n**4. 为什么不选另一种做法**\ny\n")
    call("progress", "card_ready")
elif "方向卡已确认" in prompt:                                 # 第 2、3 段：干完开 PR
    call("emit", "finding", "顺手发现一个口径问题", "--evidence", "weekly.py:43")
    call("progress", "pr_opened")
'''

GH = r'''#!/usr/bin/env python3
import json, os, sys, pathlib
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
    print(json.dumps([{"number": 40 + int(head.rsplit("T", 1)[-1])}]))
elif args[:2] == ["pr", "view"]:
    pr = args[2]
    print(json.dumps({"state": "MERGED" if merged(pr) else "OPEN", "mergedAt": None,
                      "mergeCommit": {"oid": "feedface" + pr} if merged(pr) else None,
                      "headRefOid": "head" + pr, "statusCheckRollup": [run]}))
'''


def wait_tick(p, key, want, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        out = p.out("tick")
        if want(out.get(key)):
            return out
        time.sleep(0.2)
    raise AssertionError(f"tick 一直没出 {key}")


@pytest.fixture
def e2e(tmp_path, monkeypatch):
    root = tmp_path / "proj"; home = root / ".claude" / "router"
    git_project(tmp_path, root); home.mkdir(parents=True)
    runs = tmp_path / "daily" / "runs"; (runs / "2026-10-26").mkdir(parents=True)
    (runs / "2026-10-26" / "sent").write_text("")
    bin_ = tmp_path / "bin"; bin_.mkdir(); (bin_ / "gh").write_text(GH); (bin_ / "gh").chmod(0o755)
    (tmp_path / "hook.py").write_text(HOOK)
    cfg = {"repo": "o/r", "base_branch": "staging", "worktree_dir": ".claude/worktrees", "map_title": "m",
           "weekly_runs_dir": str(runs), "lark_config": str(tmp_path / "none.json"), "leak_guard_globs": [],
           "slots": {"heavy": 3, "light": 2}, "kickoff_budget_min": 30, "stale_min": 45, "disk_limit_gb": 150,
           "claude_projects_dir": str(tmp_path / "projects"),
           "worker": {"model": "m", "permission_mode": "bypassPermissions", "allowed_tools": [], "disallowed_tools": [],
                      "claude_bin": [sys.executable, str(FAKE)]}}
    (home / "config.json").write_text(json.dumps(cfg))
    shutil.copy(FIX / "surfaces.json", home / "surfaces.json")
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("FAKE_CLAUDE_HOOK", str(tmp_path / "hook.py"))
    monkeypatch.delenv("ROUTER_HOME", raising=False); monkeypatch.delenv("ROUTER_TICKET", raising=False)

    class P:
        pass
    p = P(); p.root, p.home, p.tmp = root, home, tmp_path
    p.out = lambda *a: json.loads(subprocess.run([sys.executable, str(CLI), "--root", str(root), *a],
                                                 capture_output=True, text=True, timeout=60).stdout)
    p.state = lambda: json.loads((home / "state.json").read_text())
    p.gh = lambda: [json.loads(l) for l in (tmp_path / "gh.log").read_text().splitlines()]
    return p


def test_full_cycle_with_fake_claude(e2e):
    tri = {"batch": "2026-10-26", "tickets": [
        {"title": "周报口径", "weight": "light", "priority": 1,
         "why": {"metric": "tells", "users": "6/30", "evidence": "~/.her-persona-daily/runs/2026-10-26/summary.json"},
         "problem": "tells 混了三类", "problem_anchor": "weekly.py:43", "clue": "拆三项", "surfaces": ["weekly-report"],
         "tags": ["周报口径"], "depends_on": [], "covered_by": "", "decision": "dispatch", "reason": "r"},
        {"title": "安全指标按周算", "weight": "light", "priority": 2,
         "why": {"metric": "claims_human", "users": "5 起", "evidence": "~/.her-persona-daily/runs/2026-10-26/summary.json"},
         "problem": "只有个案列表", "problem_anchor": "weekly.py:541", "clue": "", "surfaces": ["weekly-report"],
         "tags": [], "depends_on": ["周报口径"], "covered_by": "", "decision": "dispatch", "reason": "r"}]}
    (e2e.tmp / "tri.json").write_text(json.dumps(tri, ensure_ascii=False))

    assert e2e.out("tick")["triage_needed"] == "2026-10-26"
    assert e2e.out("draft", "--from", str(e2e.tmp / "tri.json"))["ok"]
    assert e2e.out("tick")["triage_needed"] is None and e2e.state()["tickets"]["T1"]["status"] == "draft"

    start = e2e.out("start", "--all")
    assert start["ok"] and start["dispatched"] == ["T1"]                      # T2 依赖 T1 + 同代码面 → 排队
    assert e2e.state()["tickets"]["T2"]["status"] == "queued"

    out = wait_tick(e2e, "cards_for_review", lambda v: v == ["T1"])
    assert e2e.state()["tickets"]["T2"]["waiting_for"] == "等 T1 合进 staging"
    card = e2e.home / "cards" / "T1.md"
    q = e2e.out("submit-card", "T1", "--card", str(card), "--rounds", "1")["queue"]
    assert e2e.state()["tickets"]["T1"]["status"] == "await_card"

    assert e2e.out("answer", q, "A 同意")["resumed"] == "T1"
    out = wait_tick(e2e, "dispatched", lambda v: v == ["T2"])               # 同一轮：合 T1 → 放锁 → 派 T2
    s = e2e.state()
    t1 = s["tickets"]["T1"]
    assert t1["status"] == "merged_staging" and t1["merge_sha"] == "feedface41" and t1["pr"] == 41
    assert s["tickets"]["T2"]["status"] == "kickoff"
    assert ["pr", "merge", "41", "--squash", "--match-head-commit", "head41", "-R", "o/r"] in e2e.gh()
    assert not any("--auto" in c for c in e2e.gh())
    comments = [c for c in e2e.gh() if c[:2] == ["issue", "comment"]]
    assert comments and comments[0][2] == str(s["tickets"]["T2"]["issue"]) and "feedface41" in comments[0][-1]
    assert not (e2e.root / ".claude/worktrees/router-T1").exists()           # 合完删 worktree
    assert [q["kind"] for q in s["queue"]] == ["handson"]
    board = (e2e.home / "board.html").read_text()
    assert "周报口径" in board and "已合 staging" in board and "假方向：拆 tells" in board
    assert "顺手发现一个口径问题" in board                                     # 事件时间线
