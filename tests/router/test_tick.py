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
    p.run = lambda: tick.run(fake, root, cfg)
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
    proj.put(T1=("verifying", ["her-reply-prompt"], {"weight": "heavy", "issue": 11, "pr": 41,
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
    assert res["dispatched"] == ["T2"] and s["tickets"]["T2"]["status"] == "kickoff"
    assert [q["kind"] for q in s["queue"]] == ["handson"]


def test_ci_failure_resumes_once_then_stuck(proj):
    proj.put(T1=("verifying", ["a"], {"pr": 41, "worker": {"session_id": "sid", "worktree": "/wt/x", "runs": 1}}))
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
