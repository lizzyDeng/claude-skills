import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

import pytest
from conftest import FIX, make_state

import events
import notify
import store
import tick

T0 = "2026-11-02T10:00:00Z"            # FakeCtx 默认时钟
MAIN_SHA = "a" * 40                    # 远端 main（以及默认 staging）的 sha


@pytest.fixture
def proj(tmp_path, fake):
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
    p = P(); p.root, p.home, p.cfg, p.ctx, p.tmp, p.runs = root, home, cfg, fake, tmp_path, runs

    def put(**tickets):
        s = make_state(**tickets)
        store.save_state(home / "state.json", s)
        return s
    p.put = put
    p.state = lambda: store.load_state(home / "state.json")
    p.run = lambda: tick.run(p.ctx, root, cfg)          # 用 p.ctx：真进程用例会把它换成 RealProcCtx
    # 默认远端 staging / main 都在（R1 的 ensure_base 会查）；要模拟缺失的用例自己覆盖
    for b in ("staging", "main"):
        fake.script(["git", "-C", str(root), "ls-remote", "--exit-code", "origin", f"refs/heads/{b}"],
                    f"{MAIN_SHA}\trefs/heads/{b}\n")
    return p


def running(status, surfaces, pid=123, started=T0, log=None, **kw):
    return (status, surfaces, {"worker": {"pid": pid, "session_id": "sid", "worktree": "/wt/x", "branch": "router/T1",
                                          "started_at": started, "log": log, "runs": 1}, **kw})


def log_with(p, tid, text):
    f = p.home / "logs" / f"{tid}-1.log"; f.parent.mkdir(exist_ok=True); f.write_text(text)
    return str(f)


def test_dead_worker_without_result_becomes_stuck(proj):
    proj.put(T1=running("executing", ["weekly-report"], log=log_with(proj, "T1", "partial output, no json")))
    res = proj.run()
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "stuck" and t["status"] not in (store.DONE, store.MERGED)
    assert t["worker"]["pid"] is None
    q = proj.state()["queue"]
    assert [x["kind"] for x in q] == ["stuck"] and res["new_queue"] == [q[0]["id"]]
    assert proj.run()["new_queue"] == []                      # 同一次死亡只报一次


def test_error_result_retries_once_then_stuck(proj):
    err = json.dumps({"type": "result", "is_error": True, "result": "API Error: 529 overloaded"})
    proj.put(T1=running("executing", ["weekly-report"], log=log_with(proj, "T1", err)))
    proj.run()
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "executing" and t["fail_streak"] == {"step": "executing", "count": 1}
    assert len(proj.ctx.spawned) == 1 and "--resume" in proj.ctx.spawned[0]["cmd"]
    proj.ctx.alive.clear()
    Path(t["worker"]["log"]).write_text(err)
    proj.run()
    assert proj.state()["tickets"]["T1"]["status"] == "stuck"


def test_card_ready_goes_to_review_not_queue(proj):
    proj.put(T1=running("kickoff", ["weekly-report"], log=log_with(proj, "T1", '{"type":"result","is_error":false}')))
    events.emit_progress(proj.home, "T1", "card_ready", "2026-11-02T10:05:00Z")
    res = proj.run()
    t = proj.state()["tickets"]["T1"]
    assert res["cards_for_review"] == ["T1"] and proj.state()["queue"] == []
    assert t["status"] == "kickoff" and t["worker"]["pid"] is None and t["card_ready_at"]
    assert proj.run()["cards_for_review"] == []


def test_old_progress_from_previous_run_is_ignored(proj):
    events.emit_progress(proj.home, "T1", "card_ready", "2026-11-02T09:00:00Z")      # 上一轮的
    proj.put(T1=running("executing", ["weekly-report"], log=log_with(proj, "T1", "")))
    proj.run()
    assert proj.state()["tickets"]["T1"]["status"] == "stuck"


def test_stop_event_queues_decision_and_frees_slot(proj):
    proj.put(T1=running("executing", ["weekly-report"], log=log_with(proj, "T1", '{"type":"result","is_error":false}')))
    events.emit(proj.home, "T1", "decision", "要跑 40 次真 LLM，约 $3", "bench.sh", tags=["spend"], at="2026-11-02T10:01:00Z")
    res = proj.run()
    t = proj.state()["tickets"]["T1"]
    q = proj.state()["queue"][0]
    assert (q["kind"], q["ticket"]) == ("spend", "T1") and "约 $3" in q["summary"]
    assert t["status"] == "paused" and t["stops"] == 1 and q["id"] in t["waiting_for"]
    assert res["slow_path_events"] == []                       # 停止事件走决定单，不走慢路径


def test_overreach_and_assumption_broken_kinds(proj):
    proj.put(T1=running("executing", ["a"], log=log_with(proj, "T1", "{}")),
             T2=running("executing", ["b"], pid=124, log=None))
    events.emit(proj.home, "T1", "overreach", "要改 her_chat/turn.rs", "x", surfaces=["her-reply-prompt"], at="2026-11-02T10:01:00Z")
    proj.state()
    s = proj.state(); s["tickets"]["T2"]["worker"]["log"] = log_with(proj, "T2", "{}"); store.save_state(proj.home / "state.json", s)
    events.emit(proj.home, "T2", "assumption_broken", "A1 不成立", "y", at="2026-11-02T10:01:00Z")
    proj.run()
    assert sorted((q["ticket"], q["kind"]) for q in proj.state()["queue"]) == [("T1", "conflict"), ("T2", "direction")]


def test_pause_requested_exit_becomes_paused(proj):
    proj.put(T1=running("executing", ["a"], log=log_with(proj, "T1", "{}"), pause_requested=True))
    proj.run()
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "paused" and not t.get("pause_requested") and proj.state()["queue"] == []


def test_kickoff_over_budget_is_flagged_and_handed_over(proj):
    proj.put(T1=running("kickoff", ["a"], kickoff_at="2026-11-02T09:29:00Z"))
    proj.ctx.alive.add(123)
    res = proj.run()
    assert res["cards_for_review"] == ["T1"] and "超时" in proj.state()["tickets"]["T1"]["waiting_for"]
    assert proj.run()["cards_for_review"] == []


def test_stale_running_worker_reported_once_unless_transcript_moves(proj):
    log = log_with(proj, "T1", "")
    old = proj.ctx.now().timestamp() - 50 * 60
    os.utime(log, (old, old))
    proj.put(T1=running("executing", ["a"], log=log, started="2026-11-02T09:00:00Z"))
    proj.ctx.alive.add(123)
    tr = proj.tmp / "projects" / "-wt-x" / "sid.jsonl"; tr.parent.mkdir(parents=True); tr.write_text("{}")
    os.utime(tr, (proj.ctx.now().timestamp() - 60, proj.ctx.now().timestamp() - 60))
    assert proj.run()["new_queue"] == []                       # transcript 1 分钟前还在动 → 没卡
    os.utime(tr, (old, old))
    res = proj.run()
    assert [q["kind"] for q in proj.state()["queue"]] == ["stuck"] and "疑似卡住" in proj.state()["queue"][0]["summary"]
    assert proj.run()["new_queue"] == []


def _pr_fake(proj, merged_after_merge=True):
    green = (FIX / "gh_pr_green.json").read_text()
    merged = (FIX / "gh_pr_merged.json").read_text()
    proj.ctx.script(["gh", "pr", "view"], lambda cmd: merged if proj.ctx.calls_with("pr", "merge") else green)


def test_merge_releases_lock_comments_downstream_and_dispatches_next(proj):
    proj.put(T1=("verifying", ["her-reply-prompt"], {"weight": "heavy", "issue": 11, "pr": 41, "card_confirmed": True,
                                                     "solution": {"kind": "card", "text": "底座改为承认是 AI", "source": "v1"},
                                                     "worker": {"worktree": str(proj.root / "wt1"), "branch": "router/T1"}}),
             T2=("queued", ["her-reply-prompt"], {"weight": "heavy", "issue": 12, "start_ok": True, "blocked_by": ["T1"]}))
    _pr_fake(proj)
    res = proj.run()
    s = proj.state()
    assert s["tickets"]["T1"]["status"] == "merged_staging" and s["tickets"]["T1"]["merge_sha"] == "deadbeef"
    assert ["gh", "pr", "merge", "41", "--squash", "--match-head-commit", "abc123", "-R", "o/r"] in proj.ctx.calls
    c = proj.ctx.calls_with("issue", "comment")
    assert c and c[0][3] == "12" and "deadbeef" in c[0][c[0].index("--body") + 1]
    assert ["git", "-C", str(proj.root), "worktree", "remove", "--force", str(proj.root / "wt1")] in proj.ctx.calls
    assert s["tickets"]["T1"]["worker"]["worktree"] is None          # R6：remove 成功就清指针（看板不再给失效的接管命令）
    assert res["dispatched"] == ["T2"] and s["tickets"]["T2"]["status"] == "kickoff"
    assert [q["kind"] for q in s["queue"]] == ["handson"]


def test_ci_failure_resumes_once_then_stuck(proj):
    proj.put(T1=("verifying", ["a"], {"pr": 41, "card_confirmed": True, "worker": {"session_id": "sid", "worktree": "/wt/x", "runs": 1}}))
    proj.ctx.script(["gh", "pr", "view"], (FIX / "gh_pr_failed.json").read_text())
    proj.run()
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "executing" and "CI" in proj.ctx.spawned[0]["cmd"][2]
    s = proj.state(); store.set_status(s, "T1", store.VERIFYING, T0); s["tickets"]["T1"]["worker"]["pid"] = None
    store.save_state(proj.home / "state.json", s)
    proj.run()
    assert proj.state()["tickets"]["T1"]["status"] == "stuck"


def test_interface_change_marks_subscribers_for_review(proj):
    ext = proj.tmp / "ext"; (ext / ".claude").mkdir(parents=True)
    s = proj.put(T1=running("executing", ["her-reply-prompt"]), T2=("await_card", ["her-reply-prompt"]))
    proj.ctx.alive.add(123)
    proj.ctx.script(["git", "-C", str(proj.root), "worktree", "list", "--porcelain"],
                    f"worktree {proj.root}\nbranch refs/heads/main\n\nworktree {ext}\nbranch refs/heads/feat/x\n")
    proj.ctx.script(["git", "-C", str(ext), "status", "--porcelain"], " M crates/persona-engine/src/prompt_builder.rs\n")
    os.utime(ext, None)
    (ext / "crates/persona-engine/src").mkdir(parents=True); (ext / "crates/persona-engine/src/prompt_builder.rs").write_text("x")
    events.emit(proj.home, "T1", "interface_changed", "底座前缀改名", "prompt_template_en.rs:48", at="2026-11-02T10:01:00Z")
    res = proj.run()
    st = proj.state()
    assert st["tickets"]["T2"]["review_flag"] is True and st["tickets"]["T1"]["review_flag"] is False
    assert "底座前缀改名" in (proj.home / "inbox" / "T2.jsonl").read_text()
    assert res["external_deliveries"][0]["worktree"] == str(ext)
    assert "底座前缀改名" in (ext / ".claude" / "router-inbox.md").read_text()
    assert proj.run()["external_deliveries"] == []             # 游标推进，不重复


def test_unrouted_finding_goes_to_slow_path(proj):
    proj.put(T1=running("executing", ["a"])); proj.ctx.alive.add(123)
    events.emit(proj.home, "T1", "finding", "周报 tells 少算了泄漏", "weekly.py:43", at="2026-11-02T10:01:00Z")
    assert [e["summary"] for e in proj.run()["slow_path_events"]] == ["周报 tells 少算了泄漏"]


def test_nothing_dispatched_before_start(proj):
    proj.put(T1=("draft", ["a"]), T2=("queued", ["b"]))       # T2 没 start_ok
    assert proj.run()["dispatched"] == [] and proj.ctx.spawned == []


def test_dispatch_creates_worktree_and_brief(proj):
    proj.put(T1=("queued", ["weekly-report"], {"start_ok": True, "issue": 7, "title": "周报口径",
                                               "problem": "tells 混了三类", "why": {"metric": "tells", "users": "6/30", "evidence": "~/x/summary.json"}}))
    res = proj.run()
    t = proj.state()["tickets"]["T1"]
    assert res["dispatched"] == ["T1"] and t["status"] == "kickoff" and t["kickoff_at"] == T0
    sp = proj.ctx.spawned[0]
    assert sp["cwd"] == str(proj.root / ".claude/worktrees/router-T1") and "--session-id" in sp["cmd"]
    assert sp["env"]["ROUTER_TICKET"] == "T1" and sp["env"]["ROUTER_HOME"] == str(proj.home)
    brief = sp["cmd"][sp["cmd"].index("-p") + 1]
    for part in ("worker.md", "#7", "tells 混了三类", "~/x/summary.json", "weekly-report", "ROUTER_TICKET"):
        assert part in brief


def test_waiting_for_explains_queue_reason(proj):
    proj.put(T1=("executing", ["her-reply-prompt"], {"weight": "heavy", "worker": {"pid": 123, "log": None}}),
             T2=("queued", ["her-reply-prompt"], {"weight": "heavy", "start_ok": True}),
             T3=("queued", ["z"], {"start_ok": True, "blocked_by": ["T1"]}))
    proj.ctx.alive.add(123)
    proj.run()
    s = proj.state()
    assert s["tickets"]["T2"]["waiting_for"] == "等代码面 her-reply-prompt（T1 持有）"
    assert s["tickets"]["T3"]["waiting_for"] == "等 T1 合进 staging"


def test_heavy_blocked_when_cargo_dir_too_big(proj):
    proj.cfg["cargo_target_dir"] = "/big"
    proj.ctx.script(["du", "-sk", "/big"], f"{160 * 1024 * 1024}\t/big\n")
    proj.put(T1=("queued", ["her-reply-prompt"], {"weight": "heavy", "start_ok": True}),
             T2=("queued", ["weekly-report"], {"start_ok": True}))
    assert proj.run()["dispatched"] == ["T2"]
    assert "CARGO_TARGET_DIR" in proj.state()["tickets"]["T1"]["waiting_for"]


def test_new_weekly_run_sets_triage_needed_once(proj):
    proj.put()
    (proj.runs / "2026-10-19").mkdir(); (proj.runs / "2026-10-19" / "sent").write_text("")
    (proj.runs / "2026-10-26").mkdir()                                     # 还没发出去
    assert proj.run()["triage_needed"] == "2026-10-19"
    (proj.runs / "2026-10-26" / "sent").write_text("")
    assert proj.run()["triage_needed"] == "2026-10-26"
    s = proj.state(); s["batch"]["id"] = "2026-10-26"; store.save_state(proj.home / "state.json", s)
    assert proj.run()["triage_needed"] is None


def test_notify_only_counts_and_ids(proj):
    (proj.tmp / "lark.json").write_text(json.dumps({"webhook_url": "https://lark/x", "sign_secret": "sec"}))
    proj.put(T1=running("executing", ["a"], log=log_with(proj, "T1", "nothing"),
                        why={"metric": "秘密指标", "users": "9", "evidence": "原话证据"}, problem="很长的问题描述"))
    res = proj.run()
    assert res["notified"] is True
    url, payload = proj.ctx.posted[-1]
    text = payload["content"]["text"]
    assert text == "router：等你 1 件（Q1）"
    assert "秘密指标" not in json.dumps(payload, ensure_ascii=False) and "很长的问题描述" not in text


def test_notify_signature_matches_weekly(fake, tmp_path):
    cfg = tmp_path / "lark.json"; cfg.write_text(json.dumps({"webhook_url": "https://lark/x", "sign_secret": "sec"}))
    assert notify.send(cfg, "hi", fake) == "sent"
    payload = fake.posted[-1][1]
    key = f"{payload['timestamp']}\nsec".encode()
    assert payload["sign"] == base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()
    assert payload["msg_type"] == "text" and payload["content"] == {"text": "hi"}
    assert notify.send(tmp_path / "missing.json", "hi", fake).startswith("DRY RUN")


def test_heavy_blocked_when_free_disk_too_low(proj):
    proj.cfg["disk_min_free_gb"] = 10 ** 9                 # 任何盘都不够 → 只派轻票
    proj.put(T1=("queued", ["her-reply-prompt"], {"weight": "heavy", "start_ok": True}),
             T2=("queued", ["weekly-report"], {"start_ok": True}))
    assert proj.run()["dispatched"] == ["T2"]
    assert "剩余" in proj.state()["tickets"]["T1"]["waiting_for"]


# ---- 终审修复 ----------------------------------------------------------------

def test_pr_opened_before_card_confirmed_is_not_merged(proj):        # C2
    proj.put(T1=running("kickoff", ["a"], log=log_with(proj, "T1", "{}")))
    events.emit_progress(proj.home, "T1", "card_ready", "2026-11-02T10:01:00Z")
    events.emit_progress(proj.home, "T1", "pr_opened", "2026-11-02T10:02:00Z")
    proj.run()
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "paused" and [q["kind"] for q in proj.state()["queue"]] == ["direction"]
    s = proj.state(); store.set_status(s, "T1", store.VERIFYING, T0); s["tickets"]["T1"]["pr"] = 41
    store.save_state(proj.home / "state.json", s)
    _pr_fake(proj)
    proj.run()
    assert not proj.ctx.calls_with("pr", "merge") and proj.state()["tickets"]["T1"]["status"] == "verifying"


def _verifying(proj, fx, **kw):
    proj.put(T1=("verifying", ["a"], {"pr": 41, "card_confirmed": True,
                                      "worker": {"session_id": "sid", "worktree": "/wt/x", "runs": 1}, **kw}))
    proj.ctx.script(["gh", "pr", "view"], (FIX / fx).read_text())


def test_pr_against_wrong_base_is_queued_not_merged(proj):            # C1
    _verifying(proj, "gh_pr_wrong_base.json")
    proj.run(); proj.run()
    assert not proj.ctx.calls_with("pr", "merge")
    assert [q["kind"] for q in proj.state()["queue"]] == ["stuck"] and "main" in proj.state()["queue"][0]["summary"]


def test_closed_pr_queues_and_clears(proj):                           # I1
    _verifying(proj, "gh_pr_closed.json")
    proj.run()
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "stuck" and t["pr"] is None and "关闭" in proj.state()["queue"][0]["summary"]


def test_conflicting_pr_resumes_worker_to_rebase(proj):               # I1
    _verifying(proj, "gh_pr_conflict.json")
    proj.run()
    assert proj.state()["tickets"]["T1"]["status"] == "executing" and "rebase" in proj.ctx.spawned[0]["cmd"][2]


def test_merge_failure_twice_queues(proj):                            # I1
    _verifying(proj, "gh_pr_green.json")
    proj.ctx.script(["gh", "pr", "merge"], "", returncode=1, stderr="Base branch was modified")
    proj.run(); assert proj.state()["queue"] == []
    proj.run(); assert [q["kind"] for q in proj.state()["queue"]] == ["stuck"]


def test_merge_waits_for_live_worker(proj):                           # I6
    _verifying(proj, "gh_pr_green.json", )
    s = proj.state(); s["tickets"]["T1"]["worker"]["pid"] = 123; store.save_state(proj.home / "state.json", s)
    proj.ctx.alive.add(123)
    proj.run()
    assert not proj.ctx.calls_with("pr", "merge")


def test_pr_with_leak_is_not_merged(proj, tmp_path):                  # C4 合并前扫 PR
    (tmp_path / "u").mkdir(); (tmp_path / "u" / "users.json").write_text(json.dumps({"5eed0001": {}}))
    proj.cfg["leak_guard_globs"] = [str(tmp_path / "u" / "*.json")]
    _verifying(proj, "gh_pr_green.json")
    proj.ctx.script(["gh", "pr", "diff"], "+ // 来自用户 5eed0001 的反馈\n")
    proj.run()
    assert not proj.ctx.calls_with("pr", "merge") and "uid" in proj.state()["queue"][0]["summary"]


def test_conflict_reported_once_even_after_answer(proj):              # I2
    ext = proj.tmp / "ext"; ext.mkdir()
    proj.put(T1=("executing", ["her-reply-prompt"], {"worker": {"pid": 123, "started_at": T0}}))
    proj.ctx.alive.add(123)
    proj.ctx.script(["git", "-C", str(proj.root), "worktree", "list", "--porcelain"],
                    f"worktree {proj.root}\nbranch refs/heads/main\n\nworktree {ext}\nbranch refs/heads/feat/x\n")
    proj.ctx.script(["git", "-C", str(ext), "status", "--porcelain"], " M crates/persona-engine/src/prompt_builder.rs\n")
    (ext / "crates/persona-engine/src").mkdir(parents=True); (ext / "crates/persona-engine/src/prompt_builder.rs").write_text("x")
    assert len(proj.run()["new_queue"]) == 1
    s = proj.state(); s["queue"] = []; store.save_state(proj.home / "state.json", s)     # 用户答完
    assert proj.run()["new_queue"] == []


def test_dispatch_is_saved_before_a_crash_later_in_the_tick(proj, monkeypatch):   # I4
    proj.put(T1=("queued", ["weekly-report"], {"start_ok": True}))
    monkeypatch.setattr(tick.board, "write", lambda *a: (_ for _ in ()).throw(SystemExit("killed")))
    with pytest.raises(SystemExit): proj.run()
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "kickoff" and t["worker"]["pid"]


def test_pending_card_resurfaces_on_first_tick(proj):                 # I5
    proj.put(T1=("kickoff", ["a"], {"card_ready_at": T0, "worker": {"pid": None, "runs": 1}}))
    assert tick.run(proj.ctx, proj.root, proj.cfg)["cards_for_review"] == []
    assert tick.run(proj.ctx, proj.root, proj.cfg, resurface=True)["cards_for_review"] == ["T1"]


def test_slow_path_events_persist_until_ack(proj):                    # I5
    proj.put(T1=running("executing", ["a"])); proj.ctx.alive.add(123)
    events.emit(proj.home, "T1", "finding", "新问题", "x", at="2026-11-02T10:01:00Z")
    assert len(proj.run()["slow_path_events"]) == 1
    assert proj.run()["slow_path_events"] == []
    assert [e["summary"] for e in tick.run(proj.ctx, proj.root, proj.cfg, resurface=True)["slow_path_events"]] == ["新问题"]
    s = proj.state(); assert len(s["slow_path"]) == 1


def test_offrepo_impl_done_awaits_check_and_keeps_lock(proj):         # I8 → R6 ⑪：仓外改完 = 待你核对，不是「已合 staging」
    proj.put(T1=running("executing", ["weekly-report"], log=log_with(proj, "T1", "{}"), card_confirmed=True, issue=944),
             T2=("queued", ["weekly-report"], {"start_ok": True, "blocked_by": ["T1"], "issue": 945}))
    events.emit_progress(proj.home, "T1", "impl_done", "2026-11-02T10:01:00Z")
    res = proj.run()
    s = proj.state()
    assert s["tickets"]["T1"]["status"] == "await_check"
    q = [q for q in s["queue"] if q["kind"] == "handson"]
    assert len(q) == 1 and q[0]["topic"] == "offrepo" and "仓外" in q[0]["summary"] and "backups" in q[0]["summary"]
    assert not proj.ctx.calls_with("worktree", "remove")               # 驳回续跑要用它当 cwd
    assert not proj.ctx.calls_with("issue", "comment")                 # 核对前不通知下游
    assert res["dispatched"] == [] and s["tickets"]["T2"]["status"] == "queued"   # 核对前持锁、不解依赖
    assert "核对" in s["tickets"]["T2"]["waiting_for"]


def test_offrepo_ticket_brief_says_backup_not_pr(proj):
    proj.put(T1=("queued", ["weekly-report"], {"start_ok": True}))
    proj.run()
    brief = proj.ctx.spawned[0]["cmd"][proj.ctx.spawned[0]["cmd"].index("-p") + 1]
    assert "仓外" in brief and "backups/T1" in brief and "impl_done" in brief


def test_pause_does_not_swallow_pr_opened(proj):
    proj.put(T1=running("executing", ["a"], log=log_with(proj, "T1", "{}"), pause_requested=True, card_confirmed=True))
    events.emit_progress(proj.home, "T1", "pr_opened", "2026-11-02T10:01:00Z")
    proj.run()
    assert proj.state()["tickets"]["T1"]["status"] == "verifying"


def test_one_bad_pr_does_not_block_others(proj):
    proj.put(T1=("verifying", ["a"], {"pr": 41, "card_confirmed": True, "worker": {}}),
             T2=("verifying", ["b"], {"pr": 42, "card_confirmed": True, "worker": {}}))
    green = (FIX / "gh_pr_green.json").read_text()
    proj.ctx.script(["gh", "pr", "view", "41"], "", returncode=1, stderr="not found")
    proj.ctx.script(["gh", "pr", "view", "42"], green)
    res = proj.run()
    assert ["gh", "pr", "merge", "42", "--squash", "--match-head-commit", "abc123", "-R", "o/r"] in proj.ctx.calls
    assert any("T1" in e for e in res["errors"])


def test_pid_reuse_counts_as_dead(proj):                              # I9
    proj.put(T1=running("executing", ["a"], log=log_with(proj, "T1", "")))
    s = proj.state(); s["tickets"]["T1"]["worker"]["pid_start"] = "Sun Nov  1 09:00:00 2026"
    store.save_state(proj.home / "state.json", s)
    proj.ctx.alive.add(123)                                     # pid 123 活着，但启动时间对不上
    proj.run()
    assert proj.state()["tickets"]["T1"]["status"] == "stuck"


# =============================================================================
# plan v4 + 用户裁决 H1–H5：先红用例
# =============================================================================
import argparse  # noqa: E402
import contextlib  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402

M = MAIN_SHA               # origin/main
Z = "f" * 40               # 认不出的 tip
Z2 = "b" * 40              # main 的后代
OLD = "c" * 40             # 上次见到的 staging


def _ls(proj, branch):
    return ["git", "-C", str(proj.root), "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"]


def _present(sha, branch="staging"):
    return (f"{sha}\trefs/heads/{branch}\n", 0, "")


MISSING = ("", 2, "")


def _seen(proj, sha=OLD):
    proj.ctx.script(["git", "-C", str(proj.root), "rev-parse", "--verify", "--quiet", "refs/remotes/origin/staging"],
                    lambda c: (f"{sha}\n", 0, "") if sha else ("", 1, ""))


def _pushes(proj):
    return proj.ctx.calls_with("push")


def _wt_adds(proj):
    return proj.ctx.calls_with("worktree", "add")


def _items(proj, topic):
    return [q for q in proj.state()["queue"] if q.get("topic") == topic]


def _dec_lines(proj):
    d = proj.home / "decisions.md"
    return [l for l in d.read_text().splitlines() if l.strip()] if d.exists() else []


def _queued(**kw):
    return ("queued", ["weekly-report"], {"start_ok": True, **kw})


def _t1_errors(res):
    return [e for e in res["errors"] if "T1" in str(e)]


# ---- R1：base 缺失 → 从 main 重建（①②） ---------------------------------------

def _missing_until_push(proj, after=M):
    proj.ctx.script(_ls(proj, "staging"), lambda c: _present(after) if _pushes(proj) else MISSING)


def test_missing_base_is_rebuilt_from_main_then_dispatched(proj):
    proj.put(T1=_queued())
    _missing_until_push(proj)
    _seen(proj)
    res = proj.run()
    push = ["git", "-C", str(proj.root), "push", "--no-verify", "--force-with-lease=refs/heads/staging:", "origin",
            f"{M}:refs/heads/staging"]
    assert push in proj.ctx.calls
    lines = _dec_lines(proj)
    assert len(lines) == 1 and "重建" in lines[0] and OLD in lines[0] and "[key:base:staging:" in lines[0]
    s = proj.state()
    assert "base_incident" not in s and s["queue"] == []
    assert s["tickets"]["T1"]["status"] == "kickoff" and res["dispatched"] == ["T1"]


def test_rebuild_failure_queues_once_and_stops_retrying(proj):
    proj.put(T1=_queued())
    proj.ctx.script(_ls(proj, "staging"), "", returncode=2)
    _seen(proj)
    proj.ctx.script(["git", "-C", str(proj.root), "push"], "", returncode=1, stderr="pre-receive hook declined")
    proj.run()
    assert len(_items(proj, "base")) == 1 and not _wt_adds(proj)
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "queued" and "staging" in t["waiting_for"] and "名额" not in t["waiting_for"]
    proj.run()
    assert len(_items(proj, "base")) == 1 and len(_pushes(proj)) == 1     # 决定单项还在 ⇒ 退避，不再推


def test_never_seen_base_is_not_created(proj):
    proj.put(T1=_queued())
    proj.ctx.script(_ls(proj, "staging"), "", returncode=2)
    _seen(proj, sha=None)
    proj.run()
    items = _items(proj, "base")
    assert not _pushes(proj) and len(items) == 1 and "从没见过" in items[0]["summary"]
    assert not _wt_adds(proj)


def test_main_missing_never_pushes(proj):
    proj.put(T1=_queued())
    proj.ctx.script(_ls(proj, "staging"), "", returncode=2)
    proj.ctx.script(_ls(proj, "main"), "", returncode=2)
    _seen(proj)
    proj.run()
    assert not proj.ctx.calls_with("fetch") and not _pushes(proj)
    items = _items(proj, "base")
    assert len(items) == 1 and "origin 也没有 main" in items[0]["summary"]


@pytest.mark.parametrize("already_logged", [False, True], ids=["decide-not-written", "decide-written-state-not-saved"])
def test_rebuild_decision_written_once_across_crash_windows(proj, already_logged):
    key = f"base:staging:2026-11-02T09:59:00Z:{M[:12]}"
    s = proj.put(T1=_queued())
    s["base_incident"] = {"key": key, "main_sha": M, "old": OLD, "at": "2026-11-02T09:59:00Z"}
    store.save_state(proj.home / "state.json", s)
    if already_logged:                          # 推了、decide 也写了，state 没来得及存就崩
        (proj.home / "decisions.md").write_text(
            f"- D1 | 2026-11-02T09:59:30Z | - | 远端 staging 不存在：从 origin/main {M[:12]} 重建 | "
            f"理由：用户 11-02 长期授权 | 依据：ls-remote 无 refs/heads/staging [key:{key}]\n")
    before = _dec_lines(proj)
    res = proj.run()
    after = _dec_lines(proj)
    if already_logged:
        assert after == before
    else:
        assert len(after) == 1 and "补记" in after[0] and f"[key:{key}]" in after[0]
    assert "base_incident" not in proj.state() and not _pushes(proj)
    assert res["dispatched"] == ["T1"]


def test_foreign_tip_after_lease_reject_blocks_dispatch(proj):
    proj.put(T1=_queued())
    _missing_until_push(proj, after=Z)                  # lease 被拒后再查：别人建了个指向 Z 的同名分支
    _seen(proj)
    proj.ctx.script(["git", "-C", str(proj.root), "push"], "", returncode=1, stderr="! [rejected] (stale info)")
    res = proj.run()
    assert not _wt_adds(proj) and res["dispatched"] == []
    items = _items(proj, "base")
    assert len(items) == 1 and Z[:12] in items[0]["summary"] and M[:12] in items[0]["summary"]
    assert not any("重建" in l for l in _dec_lines(proj))


@pytest.mark.parametrize("ancestor_rc", [1, 0], ids=["Z-not-descendant", "Z-descends-from-M"])
def test_incident_recovery_with_foreign_tip_blocks_dispatch(proj, ancestor_rc):
    s = proj.put(T1=_queued())
    s["base_incident"] = {"key": f"base:staging:2026-11-02T09:59:00Z:{M[:12]}", "main_sha": M, "old": OLD,
                          "at": "2026-11-02T09:59:00Z"}
    store.save_state(proj.home / "state.json", s)
    proj.ctx.script(_ls(proj, "staging"), *_present(Z)[:2])
    proj.ctx.script(["git", "-C", str(proj.root), "merge-base", "--is-ancestor", M, Z], "", returncode=ancestor_rc)
    res = proj.run()
    st = proj.state()
    assert "base_incident" not in st
    if ancestor_rc == 1:
        assert len(_items(proj, "base")) == 1 and res["dispatched"] == [] and not _wt_adds(proj)
        assert st.get("base_problem")                                  # incident 已转成 base_problem
    else:
        assert any("补记" in l for l in _dec_lines(proj)) and res["dispatched"] == ["T1"]


@pytest.mark.parametrize("rc", [128, 124], ids=["unreachable", "timeout"])
def test_base_check_network_error_never_rebuilds(proj, rc):
    proj.put(T1=_queued())
    proj.ctx.script(_ls(proj, "staging"), "", returncode=rc, stderr="fatal: unable to access 'https://x/'")
    res = proj.run()
    st = proj.state()
    assert not _pushes(proj) and not _wt_adds(proj) and res["dispatched"] == []
    assert st["queue"] == []                                           # 弱网不进决定单
    assert any("staging" in str(e) or "base" in str(e) for e in res["errors"])
    assert st["tickets"]["T1"]["status"] == "queued" and "staging" in st["tickets"]["T1"]["waiting_for"]


# ---- H1：重建失败后远端出现 Z≠M；回答 foreign_tip 之后 tip 仍是 Z -----------------

@pytest.mark.parametrize("tip,ancestor_rc,blocked", [(Z, 1, True), (M, None, False), (Z2, 0, False)],
                         ids=["foreign-Z", "back-at-M", "descendant-of-M"])
def test_rebuild_failure_then_foreign_tip_appears_blocks_dispatch(proj, tip, ancestor_rc, blocked):
    proj.put(T1=_queued())
    phase = {"n": 1}
    proj.ctx.script(_ls(proj, "staging"), lambda c: MISSING if phase["n"] == 1 else _present(tip))
    _seen(proj)
    proj.ctx.script(["git", "-C", str(proj.root), "push"], "", returncode=1, stderr="pre-receive hook declined")
    proj.run()
    assert len(_items(proj, "base")) == 1 and proj.state()["tickets"]["T1"]["status"] == "queued"
    phase["n"] = 2                                                       # 下一轮：远端又出现了 staging
    if ancestor_rc is not None:
        proj.ctx.script(["git", "-C", str(proj.root), "merge-base", "--is-ancestor", M, tip], "", returncode=ancestor_rc)
    res = proj.run()
    if blocked:                                                          # 必须验 S==M 或 M 是 S 的祖先
        items = _items(proj, "base")
        assert not _wt_adds(proj) and res["dispatched"] == []
        assert len(items) == 1 and Z[:12] in items[0]["summary"]
    else:
        assert res["dispatched"] == ["T1"] and _items(proj, "base") == []


@pytest.mark.parametrize("tip_after_answer,blocked", [(Z, True), (M, False)], ids=["still-foreign", "fixed-to-M"])
def test_answer_foreign_tip_then_tip_still_foreign_keeps_blocking(proj, tip_after_answer, blocked):
    import router
    proj.put(T1=_queued())
    _missing_until_push(proj, after=Z)
    _seen(proj)
    proj.ctx.script(["git", "-C", str(proj.root), "push"], "", returncode=1, stderr="! [rejected] (stale info)")
    proj.run()
    items = _items(proj, "base")
    assert len(items) == 1 and Z[:12] in items[0]["summary"]
    (proj.home / "config.json").write_text(json.dumps(proj.cfg))
    out = router.cmd_answer(argparse.Namespace(qid=items[0]["id"], text="好了", reject=False),
                            router.Env(proj.root, proj.ctx))
    assert out["ok"] and not [q for q in proj.state()["queue"] if q["id"] == items[0]["id"]]
    proj.ctx.script(_ls(proj, "staging"), *_present(tip_after_answer)[:2])
    proj.ctx.script(["git", "-C", str(proj.root), "merge-base", "--is-ancestor", M, Z], "", returncode=1)
    res = proj.run()
    if blocked:                                   # 回答不等于接受当前 tip：重验仍是 Z ⇒ 仍不派、重新进决定单
        again = _items(proj, "base")
        assert not _wt_adds(proj) and res["dispatched"] == []
        assert len(again) == 1 and Z[:12] in again[0]["summary"]
    else:
        assert res["dispatched"] == ["T1"]


# ---- R1：被 GitHub 自动改指的 router PR 改回 base -----------------------------------

def _timeline(*nodes):
    return json.dumps({"data": {"repository": {"pullRequest": {"timelineItems": {"nodes": list(nodes)}}}}})


AUTO = {"__typename": "AutomaticBaseChangeSucceededEvent", "oldBase": "staging", "newBase": "main",
        "createdAt": "2026-11-02T13:20:00Z"}
HUMAN = {"__typename": "BaseRefChangedEvent", "previousRefName": "staging", "currentRefName": "main",
         "createdAt": "2026-11-02T13:30:00Z", "actor": {"login": "someone"}}


def test_auto_retargeted_router_pr_is_moved_back_not_merged(proj):
    _verifying(proj, "gh_pr_wrong_base.json")
    proj.ctx.script(["gh", "api", "graphql"], _timeline(AUTO))
    proj.run()
    assert ["gh", "pr", "edit", "41", "-R", "o/r", "--base", "staging"] in proj.ctx.calls
    assert not proj.ctx.calls_with("pr", "merge")
    lines = _dec_lines(proj)
    assert len(lines) == 1 and "改回 staging" in lines[0]
    assert proj.state()["queue"] == [] and proj.state()["tickets"]["T1"]["status"] == "verifying"


def test_retarget_fails_twice_queues_topic_retarget_once(proj):
    _verifying(proj, "gh_pr_wrong_base.json")
    proj.ctx.script(["gh", "api", "graphql"], _timeline(AUTO))
    proj.ctx.script(["gh", "pr", "edit"], "", returncode=1, stderr="HTTP 422")
    r1 = proj.run()
    assert len(_t1_errors(r1)) == 1 and proj.state()["queue"] == []
    proj.run()
    q = proj.state()["queue"]
    assert len(q) == 1 and (q[0]["kind"], q[0].get("topic"), q[0]["ticket"]) == ("stuck", "retarget", "T1")
    r3 = proj.run()
    assert len(proj.ctx.calls_with("pr", "edit")) == 2 and len(proj.state()["queue"]) == 1 and r3["new_queue"] == []
    assert proj.state()["tickets"]["T1"]["status"] == "verifying"
    assert not proj.ctx.calls_with("pr", "merge")


def test_human_base_change_to_main_is_not_overridden(proj):            # 护栏：HEAD 上就绿
    _verifying(proj, "gh_pr_wrong_base.json")
    proj.ctx.script(["gh", "api", "graphql"], _timeline(AUTO, HUMAN))  # 最近一次是人手改的
    proj.run(); proj.run()
    assert not proj.ctx.calls_with("pr", "edit") and not proj.ctx.calls_with("pr", "merge")
    q = proj.state()["queue"]
    assert [x["kind"] for x in q] == ["stuck"] and "main" in q[0]["summary"]


# ---- R2：派单事务的故障边界（③⑤） ------------------------------------------------

def test_spawn_oserror_becomes_structured_dispatch_failure(proj):
    proj.put(T1=_queued())
    proj.ctx.spawn_error = OSError(2, "No such file or directory")
    r1 = proj.run()
    df = r1["dispatch_failed"]
    assert len(df) == 1 and set(df[0]) == {"ticket", "phase", "error"}
    assert (df[0]["ticket"], df[0]["phase"]) == ("T1", "spawn") and "No such file" in df[0]["error"]
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "queued" and t["dispatch_fail"]["count"] == 1 and "dispatch" not in t
    assert t["waiting_for"].startswith("派发失败（第 1 次")
    assert any("T1" in str(e) and "spawn" in str(e) for e in r1["errors"])
    assert len(proj.ctx.spawn_attempts) == 1
    proj.run()
    items = _items(proj, "dispatch")
    assert len(proj.ctx.spawn_attempts) == 2 and len(items) == 1 and items[0]["ticket"] == "T1"
    proj.run()
    assert len(proj.ctx.spawn_attempts) == 2 and len(_items(proj, "dispatch")) == 1    # 升级后不再试


def test_intent_save_failure_spawns_nothing_and_is_structured(proj, monkeypatch):
    proj.put(T1=_queued(), T2=("queued", ["llm-config"], {"start_ok": True}))
    real_save = store.save_state

    def flaky(path, state):
        if "dispatch" in state["tickets"].get("T1", {}):
            raise OSError(28, "No space left on device")
        real_save(path, state)
    monkeypatch.setattr(store, "save_state", flaky)
    res = proj.run()
    assert proj.ctx.spawn_attempts == []                                # 意图没落盘 ⇒ 一个进程都不起
    assert len(res["dispatch_failed"]) == 1                             # 本轮不再派 T2
    assert (res["dispatch_failed"][0]["ticket"], res["dispatch_failed"][0]["phase"]) == ("T1", "intent")
    t = proj.state()["tickets"]["T1"]
    assert "dispatch" not in t and t["worker"]["runs"] == 0 and t["dispatch_fail"]["count"] == 1


def _write_receipt(path, sid, pid, pid_start):
    """模拟启动 wrapper：exec claude 之前原子写入的启动回执（H4）。"""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": sid, "pid": pid, "pid_start": pid_start}))


def test_crash_on_save_after_spawn_never_starts_second_worker(proj, monkeypatch):   # A3 #6（H4：靠回执认领）
    proj.put(T1=_queued(), T2=("queued", ["llm-config"], {"start_ok": True}))
    real_save = store.save_state

    def crash(path, state):
        if state["tickets"]["T1"]["status"] == store.KICKOFF:
            raise OSError(5, "Input/output error")
        real_save(path, state)
    monkeypatch.setattr(store, "save_state", crash)
    with contextlib.suppress(OSError):
        proj.run()
    t1 = [x for x in proj.ctx.spawn_attempts if x["env"].get("ROUTER_TICKET") == "T1"]
    assert len(t1) == 1 and not [x for x in proj.ctx.spawn_attempts if x["env"].get("ROUTER_TICKET") == "T2"]
    monkeypatch.setattr(store, "save_state", real_save)
    disk = proj.state()["tickets"]["T1"]
    assert disk["status"] == "queued" and disk["dispatch"]["phase"] == "spawning"     # 盘上只有意图
    pid = proj.ctx.spawned[0]["pid"]
    _write_receipt(disk["dispatch"]["receipt"], disk["dispatch"]["session_id"], pid, proj.ctx.proc_start(pid))
    res = proj.run()
    assert len([x for x in proj.ctx.spawn_attempts if x["env"].get("ROUTER_TICKET") == "T1"]) == 1   # 绝不重起
    st = proj.state()
    assert st["tickets"]["T1"]["status"] == "kickoff" and st["tickets"]["T1"]["worker"]["pid"] == pid
    assert "dispatch" not in st["tickets"]["T1"]
    assert "T2" in res["dispatched"] and st["tickets"]["T2"]["status"] == "kickoff"    # T2 这一轮才派


INTENT_AT = "2026-11-02T09:58:00Z"


def _intent(proj, receipt=True, log_text=None):
    L = proj.home / "logs" / "T1-1.log"
    R = proj.home / "logs" / "T1-1.receipt.json"
    s = proj.put(T1=("queued", ["weekly-report"], {"start_ok": True, "worker": {
        "session_id": "sid-a", "worktree": str(proj.root / ".claude/worktrees/router-T1"), "branch": "router/T1", "runs": 1}}))
    s["tickets"]["T1"]["dispatch"] = {"phase": "spawning", "session_id": "sid-a", "log": str(L), "receipt": str(R),
                                      "at": INTENT_AT}
    store.save_state(proj.home / "state.json", s)
    if receipt:
        _write_receipt(R, "sid-a", 10042, "start-10042")
    if log_text is not None:
        L.parent.mkdir(parents=True, exist_ok=True); L.write_text(log_text)
    return L, R


def test_dispatch_intent_with_live_worker_is_adopted_not_respawned(proj):
    L, _ = _intent(proj)
    proj.ctx.alive.add(10042)
    proj.run()
    assert proj.ctx.spawn_attempts == []
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "kickoff" and "dispatch" not in t
    w = t["worker"]
    assert (w["pid"], w["pid_start"], w["log"], w["started_at"]) == (10042, "start-10042", str(L), INTENT_AT)
    assert t["kickoff_at"] == INTENT_AT                                  # 用意图的时间，不是认领这一刻
    assert 10042 in proj.ctx.alive


def test_dispatch_intent_with_exited_worker_goes_through_exit_handling(proj):
    _intent(proj, log_text='{"type":"result","is_error":false}')        # 回执在、进程已退出
    events.emit_progress(proj.home, "T1", "card_ready", "2026-11-02T09:59:00Z")
    res = proj.run()
    assert proj.ctx.spawn_attempts == []
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "kickoff" and "dispatch" not in t and res["cards_for_review"] == ["T1"]


def test_dispatch_intent_without_trace_is_cleared_and_retried(proj):
    _intent(proj, receipt=False)                                         # 没回执、没日志、没 transcript ⇒ 从没起过
    proj.run()
    assert len(proj.ctx.spawned) == 1
    cmd = proj.ctx.spawned[0]["cmd"]
    assert cmd[cmd.index("--session-id") + 1] == "sid-a"                 # sid 没被用过，复用
    t = proj.state()["tickets"]["T1"]
    assert "dispatch" not in t and t["status"] == "kickoff"


@pytest.mark.parametrize("how", ["lstart_unavailable", "receipt_corrupt"])
def test_ps_failure_keeps_intent_and_never_spawns(proj, how):           # A3 #9（H4：核实不了身份 ⇒ 不派、不杀）
    _, R = _intent(proj)
    if how == "lstart_unavailable":                                      # pid 活着，但 ps 查不到启动时间
        proj.ctx.alive.add(10042)
        proj.ctx.starts = {10042: None}
    else:
        R.write_text('{"session_id": "sid-a", "pid": 100')
    r1 = proj.run()
    r2 = proj.run()
    assert proj.ctx.spawn_attempts == []
    t = proj.state()["tickets"]["T1"]
    assert t["dispatch"]["session_id"] == "sid-a" and t["status"] == "queued"
    assert "核实 worker 是否已起" in t["waiting_for"]
    assert len(_t1_errors(r1)) == 1 and _t1_errors(r2) == []            # 错误只报一次
    if how == "lstart_unavailable":
        assert 10042 in proj.ctx.alive                                   # 不杀


def test_dispatch_failure_is_explained_not_blamed_on_slots(proj):
    proj.put(T1=_queued())
    proj.ctx.script(["git", "-C", str(proj.root), "worktree", "add"], "", returncode=128, stderr="fatal: already exists")
    proj.run()
    w = proj.state()["tickets"]["T1"]["waiting_for"]
    assert w.startswith("派发失败") and "等轻名额" not in w


def test_dispatch_failure_twice_goes_to_queue_once(proj):
    proj.put(T1=_queued())
    proj.ctx.script(["git", "-C", str(proj.root), "worktree", "add"], "", returncode=128, stderr="fatal: already exists")
    r1 = proj.run()
    assert len(_t1_errors(r1)) == 1 and proj.state()["queue"] == []
    proj.run()
    items = _items(proj, "dispatch")
    assert len(items) == 1 and items[0]["ticket"] == "T1"
    proj.run()
    assert len(_wt_adds(proj)) == 2 and len(_items(proj, "dispatch")) == 1
    w = proj.state()["tickets"]["T1"]["waiting_for"]
    assert w.startswith("派发失败") and items[0]["id"] in w                 # 「派发失败，等你回答 Qn」
    import router                                                            # 用户回答 ⇒ 清零，下一轮重派
    (proj.home / "config.json").write_text(json.dumps(proj.cfg))
    out = router.cmd_answer(argparse.Namespace(qid=items[0]["id"], text="Q 重派", reject=False),
                            router.Env(proj.root, proj.ctx))
    assert "下一轮重派" in out["hint"] and "dispatch_fail" not in proj.state()["tickets"]["T1"]
    proj.ctx.script(["git", "-C", str(proj.root), "worktree", "add"], "", returncode=0)
    res = proj.run()
    t = proj.state()["tickets"]["T1"]
    assert res["dispatched"] == ["T1"] and t["status"] == "kickoff" and "dispatch_fail" not in t


def test_dispatch_intent_proc_start_failure_after_spawn_is_swallowed(proj, monkeypatch):
    proj.put(T1=_queued())

    def boom(pid):
        raise RuntimeError("ps: lstart unavailable")
    monkeypatch.setattr(proj.ctx, "proc_start", boom)
    res = proj.run()                                                          # 进程已经起了：查不到启动时间不算派发失败
    t = proj.state()["tickets"]["T1"]
    assert res["dispatched"] == ["T1"] and res["dispatch_failed"] == []
    assert t["status"] == "kickoff" and t["worker"]["pid"] == 10000 and t["worker"].get("pid_start") is None
    assert "dispatch" not in t and "dispatch_fail" not in t


def test_waiting_for_names_slots_only_when_full(proj):                  # 护栏：HEAD 上就绿
    proj.put(T1=running("executing", ["a"], pid=123), T2=running("executing", ["b"], pid=124),
             T3=("queued", ["c"], {"start_ok": True}))
    proj.ctx.alive |= {123, 124}
    proj.run()
    assert proj.state()["tickets"]["T3"]["waiting_for"] == "等轻名额"
    proj.put(T1=running("executing", ["a"], pid=123), T3=("queued", ["c"], {"start_ok": True, "blocked_by": ["T1"]}))
    proj.run()
    w = proj.state()["tickets"]["T3"]["waiting_for"]
    assert w == "等 T1 合进 staging" and "名额" not in w


# ---- H4：真进程验证启动回执（不靠 argv，不扫 ps） ---------------------------------

SLEEPER = ["/bin/sh", "-c", "exec sleep 30", "router-fake-claude"]      # exec 之后 argv 只剩 `sleep 30`


@pytest.fixture
def realproj(proj):
    from conftest import RealProcCtx
    rc = RealProcCtx()
    rc.scripted = list(proj.ctx.scripted)
    proj.ctx = rc
    proj.cfg["worker"]["claude_bin"] = SLEEPER
    (proj.root / ".claude" / "worktrees" / "router-T1").mkdir(parents=True)
    yield proj
    for sp in rc.spawned:
        rc.kill(sp["pid"])


def _ps_command(pid):
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout


def _wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    raise AssertionError("timeout")


def _crash_after_spawn(p, monkeypatch):
    p.put(T1=_queued())
    real_save = store.save_state

    def crash(path, state):
        if state["tickets"]["T1"]["status"] == store.KICKOFF:
            raise OSError(5, "Input/output error")
        real_save(path, state)
    monkeypatch.setattr(store, "save_state", crash)
    with contextlib.suppress(OSError):
        p.run()
    monkeypatch.setattr(store, "save_state", real_save)
    assert len(p.ctx.spawned) == 1
    pid = p.ctx.spawned[0]["pid"]
    intent = p.state()["tickets"]["T1"]["dispatch"]
    receipt = Path(intent["receipt"])
    _wait(receipt.exists)                                                 # 下一轮 tick 在 60 秒后，回执早写好了
    r = json.loads(receipt.read_text())
    assert r["pid"] == pid and r["session_id"] == intent["session_id"]
    assert r["pid_start"] and r["pid_start"] == p.ctx.proc_start(pid)    # 复用 worker_alive：pid + 进程启动时间
    return pid, intent


def test_receipt_live_real_process_is_adopted_not_respawned(realproj, monkeypatch):
    p = realproj
    pid, intent = _crash_after_spawn(p, monkeypatch)
    _wait(lambda: "sleep" in _ps_command(pid))
    assert intent["session_id"] not in _ps_command(pid)                  # argv 里根本没有 session id
    p.run()
    assert len(p.ctx.spawned) == 1                                        # 回执在、进程活 ⇒ 认领，不重起
    t = p.state()["tickets"]["T1"]
    assert t["status"] == "kickoff" and t["worker"]["pid"] == pid and "dispatch" not in t
    assert p.ctx.pid_alive(pid)


def test_receipt_dead_real_process_goes_through_exit_handling(realproj, monkeypatch):
    p = realproj
    pid, _ = _crash_after_spawn(p, monkeypatch)
    p.ctx.kill(pid)
    _wait(lambda: not p.ctx.pid_alive(pid))
    p.run()
    assert len(p.ctx.spawned) == 1                                        # 回执在、进程死 ⇒ 走退出处理，不重起
    t = p.state()["tickets"]["T1"]
    assert "dispatch" not in t and t["worker"]["pid"] is None and t["worker"].get("exit")
    assert t["status"] == "stuck" and [q["kind"] for q in p.state()["queue"]] == ["stuck"]


# ---- R4 / H5：Lark outbox ----------------------------------------------------------

def test_lark_failure_retried_next_tick_without_duplicate(proj):         # A3 #11
    (proj.tmp / "lark.json").write_text(json.dumps({"webhook_url": "https://lark/x"}))
    proj.put(T1=running("executing", ["a"], log=log_with(proj, "T1", "nothing")))   # 死掉的 worker ⇒ 一条 stuck
    proj.ctx.post_errors = [urllib.error.URLError("network down")]
    proj.run()
    s = proj.state()
    assert len(s["queue"]) == 1 and s["queue"][0]["notified_at"] is None and s["notify_error"]["count"] == 1
    assert len(proj.ctx.post_attempts) == 1
    proj.run()
    qid = proj.state()["queue"][0]["id"]
    assert len(proj.ctx.post_attempts) == 2 and len(proj.ctx.posted) == 1
    assert qid in proj.ctx.posted[0][1]["content"]["text"]
    s = proj.state()
    assert s["queue"][0]["notified_at"] and "notify_error" not in s
    proj.run()
    assert len(proj.ctx.post_attempts) == 2                              # 成功后不重复


def test_lark_configured_but_missing_keeps_pending_and_retries(proj):    # H5
    # 未配置（开发态）：DRY RUN 算送达
    proj.cfg.pop("lark_config")
    proj.put(T1=running("executing", ["a"], log=log_with(proj, "T1", "nothing")))
    proj.run()
    q = proj.state()["queue"]
    assert len(q) == 1 and q[0]["notified_at"] and proj.ctx.post_attempts == []
    # 配置了路径、文件临时缺失（故障态）：保持 pending、记 notify_error，下一轮重试
    lark = proj.tmp / "lark-configured.json"
    proj.cfg["lark_config"] = str(lark)
    proj.put(T1=running("executing", ["a"], log=log_with(proj, "T1", "nothing")))
    proj.run()
    s = proj.state()
    assert s["queue"][0]["notified_at"] is None and s["notify_error"]["count"] == 1
    proj.run()
    s = proj.state()
    assert s["queue"][0]["notified_at"] is None and s["notify_error"]["count"] == 2
    lark.write_text(json.dumps({"webhook_url": "https://lark/x"}))       # 文件回来了
    proj.run()
    s = proj.state()
    assert len(proj.ctx.posted or []) == 1 and s["queue"][0]["id"] in proj.ctx.posted[0][1]["content"]["text"]
    assert s["queue"][0]["notified_at"] and "notify_error" not in s
    proj.run()
    assert len(proj.ctx.posted) == 1
    # 配置文件坏了（不是 JSON）：同样按失败处理，不当成「未配置」
    lark.write_text("{broken")
    proj.put(T1=running("executing", ["a"], log=log_with(proj, "T1", "nothing")))
    proj.run()
    s = proj.state()
    assert s["queue"][0]["notified_at"] is None and s["notify_error"]["count"] == 1


# ---- R6：仓外票收尾，逐目标记账（⑫） ----------------------------------------------

def _view(state="OPEN", *bodies):
    return json.dumps({"state": state, "comments": [{"body": b} for b in bodies if b]})


def _closeout(downstream, **kw):
    return {"downstream": downstream, "issue": "pending", "worktree": "done", "branch": "done", **kw}


def test_downstream_closeout_per_target_retries_only_failed_target(proj):   # A3 #12
    proj.put(T1=("done", ["weekly-report"], {"issue": 944, "title": "周报口径", "closeout": _closeout(
                 {"T2": {"issue": 945, "state": "pending"}, "T3": {"issue": 946, "state": "pending"}})}),
             T2=("queued", ["weekly-report"], {"issue": 945}), T3=("queued", ["weekly-report"], {"issue": 946}))
    for n in (944, 945, 946):
        proj.ctx.script(["gh", "issue", "view", str(n)], _view("OPEN"))
    n946 = {"n": 0}

    def c946(cmd):
        n946["n"] += 1
        return ("", 1, "HTTP 502") if n946["n"] == 1 else ("", 0, "")
    proj.ctx.script(["gh", "issue", "comment", "946"], c946)
    proj.run()
    co = proj.state()["tickets"]["T1"]["closeout"]
    assert co["downstream"]["T2"]["state"] == "done" and co["downstream"]["T3"]["state"] == "pending"
    mark = len(proj.ctx.calls)
    proj.run()
    assert not [c for c in proj.ctx.calls[mark:] if c[:4] == ["gh", "issue", "view", "945"]]    # 成功的目标不再查
    c945 = [c for c in proj.ctx.calls if c[:4] == ["gh", "issue", "comment", "945"]]
    assert len(c945) == 1 and "<!-- router:downstream:T1->T2 -->" in c945[0][c945[0].index("--body") + 1]
    assert len([c for c in proj.ctx.calls if c[:4] == ["gh", "issue", "comment", "946"]]) == 2   # 1 次失败 + 1 次成功
    assert proj.state()["tickets"]["T1"]["closeout"]["downstream"]["T3"]["state"] == "done"


def test_closeout_partial_failure_never_double_comments(proj):
    proj.put(T1=("done", ["weekly-report"], {"issue": 944, "closeout": _closeout({})}))

    def view944(cmd):
        done = [c for c in proj.ctx.calls if c[:4] == ["gh", "issue", "comment", "944"]]
        return (_view("OPEN", done[0][done[0].index("--body") + 1] if done else None), 0, "")
    proj.ctx.script(["gh", "issue", "view", "944"], view944)
    n = {"close": 0}

    def close944(cmd):
        n["close"] += 1
        return ("", 1, "HTTP 502") if n["close"] == 1 else ("", 0, "")
    proj.ctx.script(["gh", "issue", "close", "944"], close944)
    proj.run()
    assert proj.state()["tickets"]["T1"]["closeout"]["issue"] != "done"
    proj.run()
    assert len([c for c in proj.ctx.calls if c[:4] == ["gh", "issue", "comment", "944"]]) == 1
    assert n["close"] == 2 and proj.state()["tickets"]["T1"]["closeout"]["issue"] == "done"


def test_closeout_guard_empty_keeps_issue_pending_without_error_spam(proj):
    proj.cfg["leak_guard_globs"] = [str(proj.tmp / "nope" / "*.json")]       # 闸失效
    proj.put(T1=("done", ["weekly-report"], {"issue": 944,
                 "worker": {"worktree": str(proj.tmp / "gone-wt"), "branch": "router/T1"},
                 "closeout": _closeout({"T2": {"issue": 945, "state": "pending"}}, worktree="pending", branch="pending")}),
             T2=("queued", ["weekly-report"], {"issue": 945}))
    proj.ctx.script(["git", "-C", str(proj.root), "rev-parse", "--verify", "--quiet", "refs/heads/router/T1"], "d" * 40 + "\n")
    proj.ctx.script(["git", "-C", str(proj.root), "rev-list", "--count"], "0\n")
    r1 = proj.run()
    r2 = proj.run()
    co = proj.state()["tickets"]["T1"]["closeout"]
    assert co["issue"] == "pending" and co["downstream"]["T2"]["state"] == "pending"     # GitHub 各栏 fail-closed
    assert not proj.ctx.calls_with("issue", "comment") and not proj.ctx.calls_with("issue", "close")
    assert co["worktree"] == "done" and co["branch"] == "done"                          # 本地清理照做
    w = proj.state()["tickets"]["T1"]["worker"]
    assert w["worktree"] is None and w["branch"] is None
    assert _t1_errors(r1) and _t1_errors(r2) == []                                      # 只在第 1 轮报


# ---- FX：终审后修复轮 ------------------------------------------------------------------------

def _t1_spawns(p, tid="T1"):
    return [x for x in p.ctx.spawn_attempts if x["env"].get("ROUTER_TICKET") == tid]


def _enospc(path, state):
    raise OSError(28, "No space left on device")


def _error_exit(proj):
    """T1 在执行中，pid 123 已报错退出 ⇒ 第 2 步要续跑它一次。"""
    L = log_with(proj, "T1", '{"type":"result","is_error":true,"result":"boom"}')
    proj.put(T1=running("executing", ["weekly-report"], pid=123, log=L))


def test_resume_when_every_save_fails_then_recovers_spawns_once(proj, monkeypatch):     # F1（RV I-1）
    _error_exit(proj)
    real = store.save_state
    monkeypatch.setattr(store, "save_state", _enospc)
    with contextlib.suppress(OSError):
        proj.run()
    monkeypatch.setattr(store, "save_state", real)
    proj.run()                                                           # watch 重启后的下一轮
    assert len(_t1_spawns(proj)) == 1                                    # 该票总共只起 1 个 worker
    t = proj.state()["tickets"]["T1"]
    assert t["worker"]["pid"] == proj.ctx.spawned[0]["pid"] and "dispatch" not in t


def test_resume_crash_after_spawn_is_adopted_by_receipt_not_respawned(proj, monkeypatch):   # F1
    _error_exit(proj)
    real = store.save_state

    def crash(path, state):                                              # 意图落得了盘；起了进程之后的存盘全失败
        if state["tickets"]["T1"]["worker"].get("pid") == 10000:
            raise OSError(28, "No space left on device")
        real(path, state)
    monkeypatch.setattr(store, "save_state", crash)
    with contextlib.suppress(OSError):
        proj.run()
    monkeypatch.setattr(store, "save_state", real)
    assert len(_t1_spawns(proj)) == 1
    disk = proj.state()["tickets"]["T1"]
    it = disk["dispatch"]                                                # 盘上留着续跑意图（带回执路径）
    assert it["phase"] == "spawning" and it["session_id"] == "sid" and it["receipt"]
    _write_receipt(it["receipt"], "sid", 10000, proj.ctx.proc_start(10000))
    proj.run()
    assert len(_t1_spawns(proj)) == 1                                    # 按回执认领，不重起
    t = proj.state()["tickets"]["T1"]
    assert t["worker"]["pid"] == 10000 and t["status"] == "executing" and "dispatch" not in t


def test_foreground_resume_crash_after_spawn_never_starts_second_worker(proj, monkeypatch):   # F1（router._resume）
    import router
    proj.put(T1=("paused", ["weekly-report"], {"card_confirmed": True,
                                                "worker": {"session_id": "sid", "worktree": str(proj.root / "wt"),
                                                           "branch": "router/T1", "runs": 1}}))
    s = proj.state()
    store.push_queue(s, "T1", "direction", "T1：要不要继续", T0)
    store.save_state(proj.home / "state.json", s)
    (proj.home / "config.json").write_text(json.dumps(proj.cfg))
    real = store.save_state

    def crash(path, state):
        if state["tickets"]["T1"]["worker"].get("pid") == 10000:
            raise OSError(28, "No space left on device")
        real(path, state)
    monkeypatch.setattr(store, "save_state", crash)
    with pytest.raises(OSError):
        router.cmd_answer(argparse.Namespace(qid="Q1", text="继续", reject=False), router.Env(proj.root, proj.ctx))
    monkeypatch.setattr(store, "save_state", real)
    assert len(_t1_spawns(proj)) == 1
    with pytest.raises(router.Fail):                                     # 用户以为没生效、再答一次：不起第二个
        router.cmd_answer(argparse.Namespace(qid="Q1", text="继续", reject=False), router.Env(proj.root, proj.ctx))
    with pytest.raises(router.Fail):
        router.cmd_tell(argparse.Namespace(ticket="T1", text="继续", resume=True), router.Env(proj.root, proj.ctx))
    it = proj.state()["tickets"]["T1"]["dispatch"]
    _write_receipt(it["receipt"], "sid", 10000, proj.ctx.proc_start(10000))
    proj.run()
    assert len(_t1_spawns(proj)) == 1
    t = proj.state()["tickets"]["T1"]
    assert t["worker"]["pid"] == 10000 and t["status"] == "executing" and "dispatch" not in t


def _unverified_intent(proj):
    """T1 带派单意图：回执在、pid 10042 活着，但 ps 拿不到启动时间 ⇒ 认不出 ⇒ 意图保留，本票不派不杀。"""
    _intent(proj)
    proj.ctx.alive.add(10042)
    proj.ctx.starts = {10042: None}
    proj.run()
    t = proj.state()["tickets"]["T1"]
    assert t["dispatch"]["phase"] == "spawning" and t["status"] == "queued"
    (proj.root / ".claude/worktrees/router-T1").mkdir(parents=True, exist_ok=True)
    (proj.home / "config.json").write_text(json.dumps(proj.cfg))


def _inbox(proj, tid="T1"):
    f = proj.home / "inbox" / f"{tid}.jsonl"
    return f.read_text().splitlines() if f.exists() else []


def test_foreground_commands_refuse_while_dispatch_intent_unverified(proj):          # F4（RV M-1）
    import router
    _unverified_intent(proj)
    env = lambda: router.Env(proj.root, proj.ctx)
    before = proj.state()["tickets"]["T1"]
    with pytest.raises(router.Fail, match="派单待核实"):
        router.cmd_tell(argparse.Namespace(ticket="T1", text="继续", resume=True), env())
    assert _inbox(proj) == []                                            # 拒绝前不投递（不然重试会投两份）
    with pytest.raises(router.Fail, match="派单待核实"):
        router.cmd_takeover(argparse.Namespace(ticket="T1"), env())      # 不给出 resume 命令
    with pytest.raises(router.Fail, match="派单待核实"):
        router.cmd_stop(argparse.Namespace(ticket="T1"), env())
    s = proj.state()
    store.push_queue(s, "T1", "stuck", "T1 卡住", T0)
    store.save_state(proj.home / "state.json", s)
    with pytest.raises(router.Fail, match="派单待核实"):                  # 会唤醒 worker 的 answer
        router.cmd_answer(argparse.Namespace(qid="Q1", text="继续", reject=False), env())
    assert len(_t1_spawns(proj)) == 0 and 10042 in proj.ctx.alive
    after = proj.state()["tickets"]["T1"]
    assert after["status"] == before["status"] == "queued" and after["dispatch"] == before["dispatch"]
    assert [q["id"] for q in proj.state()["queue"]] == ["Q1"]            # 回答没被吞
    router.cmd_tell(argparse.Namespace(ticket="T1", text="只是留言", resume=False), env())   # 只投递收件箱：照常
    assert len(_inbox(proj)) == 1


def _live_executing(proj):
    spec = running("executing", ["weekly-report"], pid=123)
    spec[2]["worker"]["pid_start"] = "start-123"
    proj.put(T1=spec)
    proj.ctx.alive.add(123)


@pytest.mark.parametrize("how", ["ps_empty", "ps_rc124"])
def test_ps_failure_is_unknown_not_dead(proj, monkeypatch, how):                      # F5（RV M-2）
    _live_executing(proj)
    if how == "ps_empty":
        proj.ctx.starts = {123: None}                                     # ps 没输出
    else:                                                                 # 真 Ctx.proc_start，ps 超时 rc=124 还带了半截输出
        import ctx as real_ctx
        monkeypatch.setattr(real_ctx.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 124, "Mon Nov  2 10:00:00 2026\n", "timeout"))
        real = real_ctx.Ctx()
        assert real.proc_start(123) is None                              # rc≠0 一律「查不到」，不拿半截输出当启动时间
        monkeypatch.setattr(proj.ctx, "proc_start", real.proc_start)
    r1 = proj.run()
    r2 = proj.run()
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "executing" and t["worker"]["pid"] == 123      # 不判退出、不清 pid
    assert proj.state()["queue"] == [] and proj.ctx.spawn_attempts == []  # 不判 stuck、不起新 worker
    assert 123 in proj.ctx.alive
    assert len(_t1_errors(r1)) == 1 and _t1_errors(r2) == []            # 错误只报一次


def _slow_worktree_then_crash_after_spawn(proj, monkeypatch, minutes=5):
    """建 worktree 花了 minutes 分钟（fetch 弱网）；起了进程之后的存盘全失败 ⇒ 盘上只留意图，没有回执。"""
    proj.put(T1=_queued())
    proj.ctx.script(["git", "-C", str(proj.root), "worktree", "add"], lambda c: proj.ctx.advance(minutes) or ("", 0, ""))
    real = store.save_state

    def crash(path, state):
        if state["tickets"]["T1"]["worker"].get("pid") == 10000:
            raise OSError(28, "No space left on device")
        real(path, state)
    monkeypatch.setattr(store, "save_state", crash)
    with contextlib.suppress(OSError):
        proj.run()
    monkeypatch.setattr(store, "save_state", real)
    assert len(_t1_spawns(proj)) == 1
    return proj.state()["tickets"]["T1"]["dispatch"]


def test_receipt_grace_counts_from_intent_write_not_tick_start(proj, monkeypatch):    # F6（RV M-3）
    it = _slow_worktree_then_crash_after_spawn(proj, monkeypatch)
    written = proj.ctx.now()
    proj.ctx.advance(0.5)                                                # 意图写下 30 秒后的下一轮：还在宽限期内
    proj.run()
    assert len(_t1_spawns(proj)) == 1                                    # 保留意图，不重派
    t = proj.state()["tickets"]["T1"]
    assert t["dispatch"]["phase"] == "spawning" and t["status"] == "queued"
    assert "启动回执还没出现" in t["dispatch"]["unverified"]
    assert it["at"] == tick._iso(written)                                # 起点 = 意图自己落盘那一刻（起进程前一刻）


def test_receipt_grace_expired_without_trace_is_cleared_and_redispatched(proj, monkeypatch):   # F6
    it = _slow_worktree_then_crash_after_spawn(proj, monkeypatch)
    proj.ctx.advance(tick.RECEIPT_GRACE_S / 60 + 1)                      # 过了宽限期，没回执、没日志、没 transcript
    proj.run()
    sp = _t1_spawns(proj)
    assert len(sp) == 2
    cmd = sp[1]["cmd"]
    assert cmd[cmd.index("--session-id") + 1] == it["session_id"]       # sid 没用过，复用
    t = proj.state()["tickets"]["T1"]
    assert "dispatch" not in t and t["status"] == "kickoff"


def _foreign_tip_open(proj):
    s = proj.state()
    store.push_queue(s, None, "stuck", f"远端 staging 指向 {Z[:12]}，不是 {M[:12]}", "2026-11-02T09:00:00Z", topic="base")
    s["base_problem"] = {"qid": "Q1", "kind": "foreign_tip", "expected_sha": M, "sha": Z, "at": "2026-11-02T09:00:00Z"}
    store.save_state(proj.home / "state.json", s)
    proj.ctx.script(_ls(proj, "staging"), _present(Z)[0])


@pytest.mark.parametrize("fx,what", [("gh_pr_green.json", "merge"), ("gh_pr_conflict.json", "rebase"),
                                     ("gh_pr_failed.json", "fix-ci")])
def test_foreign_tip_in_queue_blocks_step5_merge_and_rebase(proj, fx, what):           # F7（RV M-4）
    _verifying(proj, fx)
    _foreign_tip_open(proj)
    proj.run()
    assert not proj.ctx.calls_with("pr", "merge")                        # 认不出的 base 上不合
    assert proj.ctx.spawn_attempts == []                                 # 也不叫 worker rebase / 修 CI
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "verifying" and "Q1" in t["waiting_for"] and "staging" in t["waiting_for"]
    assert [q["id"] for q in proj.state()["queue"]] == ["Q1"]


def _add_ticket(proj, tid, spec):
    s = proj.state()
    s["tickets"][tid] = make_state(**{tid: spec})["tickets"][tid]
    store.save_state(proj.home / "state.json", s)


def test_unverified_intent_holds_its_surface_lock(proj):                                 # F8（RV M-5）
    _unverified_intent(proj)                                             # T1 带意图（weekly-report），可能在跑
    _add_ticket(proj, "T2", ("queued", ["weekly-report"], {"start_ok": True}))
    res = proj.run()
    assert "T2" not in res["dispatched"] and len(_t1_spawns(proj, "T2")) == 0
    t2 = proj.state()["tickets"]["T2"]
    assert t2["status"] == "queued" and "weekly-report" in t2["waiting_for"] and "T1" in t2["waiting_for"]


def test_unverified_intent_occupies_a_slot(proj):                                         # F8
    proj.cfg["slots"] = {"heavy": 3, "light": 1}
    _unverified_intent(proj)
    _add_ticket(proj, "T2", ("queued", ["llm-config"], {"start_ok": True}))
    res = proj.run()
    assert "T2" not in res["dispatched"] and len(_t1_spawns(proj, "T2")) == 0
    assert proj.state()["tickets"]["T2"]["waiting_for"] == "等轻名额"
    import scheduler
    import surfaces
    s = proj.state()
    assert scheduler.free_slots(s, {"heavy": 3, "light": 1})["light"] == 0
    assert surfaces.held_locks(s).get("weekly-report") == "T1"


def test_tick_refuses_to_merge_when_base_branch_is_main(proj):                           # F10（RV M-7）
    proj.cfg["base_branch"] = "main"
    _verifying(proj, "gh_pr_wrong_base.json")                             # PR 指向 main、CI 全绿
    res = proj.run()
    assert not proj.ctx.calls_with("pr", "merge") and not _pushes(proj)
    assert any("base_branch" in e for e in res["errors"])
    assert proj.state()["tickets"]["T1"]["status"] == "verifying"
