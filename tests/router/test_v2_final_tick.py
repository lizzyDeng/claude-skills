"""v2 终审修复（巡检侧，假 ctx）：上 prod 唤醒、关票不复活、代码面空按重票、收尾删旧尝试分支、待样本满 7 天判没入口。
夹具全是合成文本，不含真实 uid 和原话。"""
from types import SimpleNamespace

from conftest import make_state
from test_tick import _pr_fake, log_with, proj, running  # noqa: F401  (proj 是 fixture)

import analyst
import events
import store
import tick


def _biz(**extra):
    base = {"kind": "business", "authorized": True, "title": "测试业务票", "issue": 11, "attempts": [], "walls": [],
            "segment": "测试人群", "no_use_streak": 0}
    return {**base, **extra}


# ---- #1 业务票合进 staging / 零代码做完 ⇒ 唤醒 router 去跑 prod-batch -----------------------------

def test_owned_merge_wakes_router_with_prod_ready(proj):
    assert "prod_ready" in tick.WAKE_KEYS
    proj.put(T1=("verifying", ["her-reply-prompt"], _biz(pr=41, card_confirmed=True,
                                                         worker={"worktree": str(proj.root / "wt1"), "branch": "router/T1"})))
    _pr_fake(proj)
    res = proj.run()
    assert proj.state()["tickets"]["T1"]["status"] == "await_prod"
    assert res["prod_ready"] == [{"ticket": "T1"}]
    assert "prod_ready" not in proj.run()                       # 只交一次


def test_owned_zero_code_impl_done_wakes_prod_ready(proj):
    proj.put(T1=running("executing", ["her-reply-prompt"], log=log_with(proj, "T1", "{}"), card_confirmed=True,
                        **_biz()))
    events.emit_progress(proj.home, "T1", "impl_done", "2026-11-02T10:01:00Z")
    res = proj.run()
    assert proj.state()["tickets"]["T1"]["status"] == "await_prod" and res["prod_ready"] == [{"ticket": "T1"}]


def test_legacy_merge_does_not_wake_prod_ready(proj):
    proj.put(T1=("verifying", ["her-reply-prompt"], {"issue": 11, "pr": 41, "card_confirmed": True,
                                                     "worker": {"worktree": str(proj.root / "wt1"), "branch": "router/T1"}}))
    _pr_fake(proj)
    res = proj.run()
    assert proj.state()["tickets"]["T1"]["status"] == "merged_staging" and "prod_ready" not in res


def test_watch_wakes_on_prod_ready(proj, monkeypatch):
    proj.put(T1=("verifying", ["her-reply-prompt"], _biz(pr=41, card_confirmed=True,
                                                         worker={"worktree": str(proj.root / "wt1"), "branch": "router/T1"})))
    _pr_fake(proj)
    monkeypatch.setattr(tick.time, "sleep", lambda s: None)
    out = tick.cmd_watch(SimpleNamespace(interval=0, max_minutes=0),     # 一轮：不醒就是 max_minutes
                         SimpleNamespace(ctx=proj.ctx, root=proj.root, cfg=proj.cfg))
    assert "prod_ready" in out["woke"] and out["prod_ready"] == [{"ticket": "T1"}]


# ---- #2 daily 的票况只列在途的票 -------------------------------------------------------------

def test_state_summary_lists_only_open_business_tickets():
    s = make_state(T1=("postlaunch", [], _biz(title="测试在途票")), T2=("done", [], _biz(title="测试已关票")),
                   T3=("skip", [], _biz(title="测试不派票")))
    for mode in ("daily", "weekly", "migrate"):
        text = analyst.state_summary(s, mode)
        assert "测试在途票" in text and "测试已关票" not in text and "测试不派票" not in text, mode


# ---- #7 业务票 / 维护票代码面为空 ⇒ 按重票：占重票名额、过磁盘闸 ------------------------------------

def test_business_ticket_without_surfaces_is_heavy_and_disk_gated(proj):
    s = store.empty_state()
    tid = store.new_business_ticket(s, title="测试业务票", segment="测试人群", want="测试想要", walls=["测试墙"],
                                    metric={"name": "测试指标"}, evidence=[], impact={}, origin="user")
    assert s["tickets"][tid]["weight"] == "heavy" and s["tickets"][tid]["surfaces"] == []
    store.set_status(s, tid, store.QUEUED, "2026-11-02T10:00:00Z")
    s["tickets"][tid].update(start_ok=True, issue=7)
    store.save_state(proj.home / "state.json", s)
    proj.cfg["disk_min_free_gb"] = 10 ** 9                     # 磁盘闸一定拦
    res = proj.run()
    t = proj.state()["tickets"][tid]
    assert res["dispatched"] == [] and t["status"] == "queued" and "磁盘" in t["waiting_for"]


# ---- #9 关业务票：收尾时删掉旧尝试的本地分支（只删已合并的；旧 checkout 还在的不碰） --------------------

def test_business_closeout_deletes_merged_old_attempt_branches(proj, tmp_path):
    kept_wt = tmp_path / "kept-wt"; kept_wt.mkdir()
    atts = [{"n": 1, "direction": "甲", "at": "x", "rollback": "r", "verdict": "useless", "branch": "router/T1",
             "merge_sha": "aaa", "pr": 41},
            {"n": 2, "direction": "乙", "at": "x", "rollback": "r", "verdict": "useless", "branch": "router/T1-a2",
             "merge_sha": "bbb", "pr": 42},
            {"n": 3, "direction": "丙", "at": "x", "rollback": "r", "verdict": "useless", "branch": "router/T1-a3",
             "merge_sha": None},
            {"n": 4, "direction": "丁", "at": "x", "rollback": "r", "verdict": "useful", "branch": "router/T1-a4",
             "merge_sha": "ddd", "pr": 44}]
    proj.put(T1=("done", [], _biz(attempts=atts, issue=None,
                                  stale_checkouts=[{"worktree": str(kept_wt), "branch": "router/T1-a2", "why": "kept"}],
                                  worker={"worktree": None, "branch": "router/T1-a4"},
                                  closeout={"downstream": {}, "issue": "n/a", "worktree": "pending", "branch": "pending",
                                            "old_branches": {"router/T1": "pending", "router/T1-a2": "pending"}})))
    for b in ("router/T1", "router/T1-a4"):
        proj.ctx.script(["git", "-C", str(proj.root), "rev-parse", "--verify", "--quiet", f"refs/heads/{b}"], "abc\n")
        proj.ctx.script(["git", "-C", str(proj.root), "rev-list", "--count", b, "--not", "--remotes"], "0\n")
    res = proj.run()
    co = proj.state()["tickets"]["T1"]["closeout"]
    assert ["git", "-C", str(proj.root), "branch", "-D", "router/T1"] in proj.ctx.calls
    assert ["git", "-C", str(proj.root), "branch", "-D", "router/T1-a4"] in proj.ctx.calls     # 当前分支照 v1 收尾
    assert ["git", "-C", str(proj.root), "branch", "-D", "router/T1-a2"] not in proj.ctx.calls  # 旧 checkout 还在：不碰
    assert ["git", "-C", str(proj.root), "branch", "-D", "router/T1-a3"] not in proj.ctx.calls  # 没合并：不删
    assert co["old_branches"]["router/T1"] == "done" and co["old_branches"]["router/T1-a2"].startswith("kept")
    assert not store.closeout_pending(co) and res["errors"] == []


def test_close_business_lists_only_merged_old_branches(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    import router
    monkeypatch.delenv("ROUTER_HOME", raising=False)
    (tmp_path / ".claude" / "router").mkdir(parents=True)
    st = make_state(T1=("postlaunch", [], _biz(attempts=[
        {"n": 1, "direction": "老票", "verdict": "legacy", "historical": True},
        {"n": 2, "direction": "甲", "verdict": "useless", "branch": "router/T1", "merge_sha": "aaa"},
        {"n": 3, "direction": "乙", "verdict": "useless", "branch": "router/T1-a2", "merge_sha": None},
        {"n": 4, "direction": "丙", "verdict": "useful", "branch": "router/T1-a3", "merge_sha": "ccc"}],
        worker={"branch": "router/T1-a3"})))
    env = router.Env(tmp_path, SimpleNamespace(now=lambda: datetime(2026, 11, 13, tzinfo=timezone.utc),
                                               pid_alive=lambda pid: False))
    router._close_business(env, st, "T1", "测试", "测试")
    assert st["tickets"]["T1"]["closeout"]["old_branches"] == {"router/T1": "pending"}   # 当前分支照 v1 的 branch 栏收


# ---- #13 待样本满 7 天、碰到改动但没人走到墙 ⇒ 判没入口，走 no_entry 唤醒 --------------------------------

def test_pending_sample_cap_with_no_wall_hits_becomes_no_entry():
    s = make_state(T1=("postlaunch", [], _biz(attempts=[
        {"n": 1, "direction": "d", "at": "x", "rollback": "r", "verdict": "pending_sample",
         "pending_since": "2026-11-01T00:00:00Z"}])))
    out = store.record_verdict(s, "T1", 1, "pending_sample", "2026-11-09T00:00:00Z", touched=4, hit_wall=0,
                               continued=0, note="测试：碰到 4 人")
    a = s["tickets"]["T1"]["attempts"][0]
    assert out["actions"] == ["check_entry"] and out["verdict"] == "no_entry" and a["verdict"] == "no_entry"
    assert "没人走到墙" in a["note"] and "pending_since" not in a
    s = make_state(T1=("postlaunch", [], _biz(attempts=[
        {"n": 1, "direction": "d", "at": "x", "rollback": "r", "verdict": "pending_sample",
         "pending_since": "2026-11-01T00:00:00Z"}])))
    try:                                                        # 有人走到墙：满 7 天有几个按几个判，不许再待样本
        store.record_verdict(s, "T1", 1, "pending_sample", "2026-11-09T00:00:00Z", touched=4, hit_wall=2,
                             continued=0, note="n")
        raise AssertionError("满 7 天、有人走到墙仍判待样本：应拒")
    except ValueError as e:
        assert "7 天" in str(e)
