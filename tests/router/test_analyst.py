"""分析 agent 运行器（Fable）：按天 / 按周起跑、收产出（apply_daily / propose_from / 叫醒 router）、失败只报一次。
夹具全是合成的：没有真实 uid、用户原话、业务数值。"""
import copy
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FIX, SCRIPTS, make_state
from test_tick import proj  # noqa: F401  （fixture）

import analyst
import launcher
import store
import tick

FAKE = Path(__file__).resolve().parent / "fake_claude.py"
CLI = SCRIPTS / "router.py"


@pytest.fixture(autouse=True)
def _calibrate_tmp(tmp_path, monkeypatch):
    """calibrate 的空 cwd 建在本测试的 tmp 下（别往系统临时目录里留目录）；仍在项目根之外。"""
    import tempfile
    sys_tmp = tmp_path / "systmp"
    sys_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(sys_tmp))
    monkeypatch.setenv("TMPDIR", str(sys_tmp))                          # 子进程（CLI 用例）也照此


def at(d, h, m, month=11):
    return datetime(2026, month, d, h, m, tzinfo=timezone.utc)


@pytest.fixture
def ap(proj):
    """test_tick 的 proj（FakeCtx）+ 配好分析 agent；时钟 01:00Z（还没到 daily 的点）。"""
    brief = proj.tmp / "analyst_brief.md"
    brief.write_text("合成的项目 brief：取数入口见此\n")
    proj.cfg["analyst"] = {"brief": str(brief)}
    proj.brief = brief
    proj.ctx.clock = at(13, 1, 0)
    return proj


@pytest.fixture
def real(ap, monkeypatch):
    """起真进程：claude_bin = fake_claude.py（OUT_JSON 决定它写什么产出）；时钟仍是假的。"""
    from conftest import RealProcCtx
    rc = RealProcCtx(now=ap.ctx.clock)
    rc.scripted = list(ap.ctx.scripted)
    ap.ctx = rc
    ap.cfg["worker"]["claude_bin"] = [sys.executable, str(FAKE)]
    monkeypatch.delenv("OUT_JSON", raising=False)
    monkeypatch.delenv("FAKE_CLAUDE_HOOK", raising=False)
    yield ap
    for sp in rc.spawned:
        rc.kill(sp["pid"])


def runs(p, mode=None):
    rs = (p.state().get("analyst") or {}).get("runs") or {}
    return {k: v for k, v in rs.items() if mode is None or v["mode"] == mode}


def only_run(p):
    (rid, r), = runs(p).items()
    return rid, r


def finish(p, rid, data):
    """FakeCtx：替分析 agent 写产出，再让进程消失。"""
    r = runs(p)[rid]
    Path(r["out"]).write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
    p.ctx.alive.discard(r["pid"])


def wait_exit(p, rid, timeout=20):
    pid = runs(p)[rid]["pid"]
    end = time.time() + timeout
    while time.time() < end:
        if not p.ctx.pid_alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError("fake claude 没退出")


def biz(title="测试业务票", **extra):
    t = {"kind": "business", "authorized": True, "title": title, "segment": "测试人群甲", "want": "测试想要甲",
         "attempts": [], "no_use_streak": 0, "card_confirmed": True,
         "walls": [{"id": "W1", "text": "测试墙一", "status": "open", "source": "analyst", "at": "2026-11-01T00:00:00Z"}]}
    t.update(extra)
    return ("postlaunch", [], t)


def weekly_dir(p, name="2026-11-09", summary=None):
    d = p.runs / name
    d.mkdir()
    (d / "sent").write_text("")
    if summary is not None:
        (d / "summary.json").write_text(json.dumps(summary))
    return d


# ---- build_cmd / render --------------------------------------------------------

def test_build_cmd_uses_fable_default():
    cfg = {"worker": {"claude_bin": "claude", "model": "worker-model", "permission_mode": "bypassPermissions",
                      "allowed_tools": ["Read", "Grep"], "disallowed_tools": ["Bash(gh pr merge*)"]}}
    cmd = analyst.build_cmd(cfg, "提示")
    assert cmd[:9] == ["claude", "-p", "提示", "--model", "claude-fable-5-1", "--output-format", "json",
                       "--permission-mode", "bypassPermissions"]
    assert cmd[9:] == ["--allowedTools", "Read,Grep", "--disallowedTools", "Bash(gh pr merge*)"]
    assert "worker-model" not in cmd                                   # 不吃 worker 的模型
    cfg["analyst"] = {"model": "claude-x"}
    cfg["worker"]["claude_bin"] = ["py", "fake.py"]
    cmd = analyst.build_cmd(cfg, "p")
    assert cmd[:3] == ["py", "fake.py", "-p"] and cmd[cmd.index("--model") + 1] == "claude-x"


def test_weekly_prompt_scopes_this_week(ap):
    """9b（finding 7）：weekly 拿到的是本周 daily 产出的清单和本周周报目录，不是通配。"""
    d = ap.home / "analyst" / "out"; d.mkdir(parents=True)
    runs = {}
    for rid, since, until in (("daily-1", "2026-10-28", "2026-10-29"), ("daily-2", "2026-11-08", "2026-11-09"),
                              ("daily-3", "2026-11-12", "2026-11-13"), ("daily-4", "2026-11-11", "2026-11-12")):
        (d / f"{rid}.json").write_text("{}")
        runs[rid] = {"mode": "daily", "out": str(d / f"{rid}.json"), "since": since, "until": until, "ingested": "x"}
    runs["daily-4"]["failed"] = True                                            # 失败的那次没有可读产出
    s = make_state(); s["analyst"] = {"runs": runs}
    store.save_state(ap.home / "state.json", s)
    wk = weekly_dir(ap, "2026-11-09")
    env = SimpleNamespace(cfg=ap.cfg, home=ap.home)
    text = analyst.render(env, "weekly", "2026-11-06", "2026-11-13", d / "w.json", week="2026-11-09")
    assert str(d / "daily-2.json") in text and str(d / "daily-3.json") in text
    assert str(d / "daily-1.json") not in text and str(d / "daily-4.json") not in text
    assert str(wk) in text and "daily-*.json" not in text
    tpl = (analyst.SKILL_DIR / "templates" / "analyst_weekly.md").read_text(encoding="utf-8")
    assert "$daily_outs" in tpl and "$weekly_dir" in tpl


def test_weekly_start_passes_week_to_prompt(ap):
    wk = weekly_dir(ap, "2026-11-09")
    ap.run()                                                                    # 新周报 ⇒ 起 weekly
    (rid, r), = runs(ap, "weekly").items()
    assert r["week"] == "2026-11-09"
    sp = ap.ctx.spawned[-1]
    assert str(wk) in sp["cmd"][sp["cmd"].index("-p") + 1]


def test_calibrate_prompt_has_no_state(ap):
    s = make_state(T1=biz("测试墙甲"), T2=("done", [], {"title": "测试老票甲"}))
    store.save_state(ap.home / "state.json", s)
    env = SimpleNamespace(cfg=ap.cfg, home=ap.home)
    out = ap.home / "analyst" / "out" / "x.json"
    cal = analyst.render(env, "calibrate", "2026-10-30", "2026-11-13", out)
    for secret in ("测试墙甲", "测试墙一", "测试人群甲", "测试老票甲"):
        assert secret not in cal, secret
    assert f"OUT={out}" in cal.splitlines() and "2026-10-30" in cal and "2026-11-13" in cal and str(ap.brief) in cal
    weekly = analyst.render(env, "weekly", "2026-11-06", "2026-11-13", out)       # 对照：其余模式看得到票
    assert "测试墙甲" in weekly and "测试墙一" in weekly and "测试人群甲" in weekly
    assert "测试老票甲" not in weekly
    mig = analyst.render(env, "migrate", "2026-10-30", "2026-11-13", out)         # migrate 还要列老票
    assert "测试墙甲" in mig and "T2" in mig and "测试老票甲" in mig and "done" in mig


def test_state_summary_shows_latest_attempt_and_open_walls_only():
    s = make_state(T1=biz(walls=[{"id": "W1", "text": "已拆的墙", "status": "broken"},
                                 {"id": "W2", "text": "还在的墙", "status": "open"}],
                          attempts=[{"n": 1, "direction": "方向一", "at": "2026-11-10T00:00:00Z", "verdict": "useful"},
                                    {"n": 2, "direction": "方向二", "at": "2026-11-12T00:00:00Z", "verdict": None}]))
    text = analyst.state_summary(s, "daily")
    assert "还在的墙" in text and "已拆的墙" not in text
    assert "方向二" in text and "方向一" not in text and "尝试 2" in text
    assert analyst.state_summary(s, "calibrate") == ""


# ---- daily ---------------------------------------------------------------------

def test_daily_runs_once_per_day(ap):
    ap.put()
    ap.ctx.clock = at(13, 1, 29)
    ap.run()
    assert ap.ctx.spawned == []                                        # 还没到 01:30
    ap.ctx.clock = at(13, 1, 31)
    ap.run(); ap.run()
    assert len(ap.ctx.spawned) == 1
    sp = ap.ctx.spawned[0]
    rid, r = only_run(ap)
    assert r["mode"] == "daily" and r["pid"] == sp["pid"] and r["log"] == sp["log"]
    assert sp["cwd"] == str(ap.home) and Path(sp["log"]).parent == ap.home / "analyst" / "logs"
    assert sp["env"]["ROUTER_HOME"] == str(ap.home)
    assert sp["cmd"][sp["cmd"].index("--model") + 1] == "claude-fable-5-1"
    prompt = sp["cmd"][sp["cmd"].index("-p") + 1]
    assert f"OUT={r['out']}" in prompt and "2026-11-12" in prompt and "2026-11-13" in prompt
    assert ap.state()["analyst"]["last_daily"] == "2026-11-13"
    finish(ap, rid, {"verdicts": [], "walls": []})
    ap.ctx.clock = at(13, 23, 59)
    ap.run()
    assert len(ap.ctx.spawned) == 1 and runs(ap)[rid]["ingested"]
    ap.ctx.clock = at(14, 1, 31)                                       # 次日：再起一个
    ap.run()
    assert len(ap.ctx.spawned) == 2 and ap.state()["analyst"]["last_daily"] == "2026-11-14"


def test_daily_at_utc_is_configurable(ap):
    ap.put()
    ap.cfg["analyst"]["daily_at_utc"] = "9:05"
    ap.ctx.clock = at(13, 9, 4)
    ap.run()
    assert ap.ctx.spawned == []
    ap.ctx.clock = at(13, 9, 5)
    ap.run()
    assert len(ap.ctx.spawned) == 1


@pytest.mark.parametrize("out_json", [None, "{not json"], ids=["no_out", "bad_json"])
def test_daily_failure_no_respawn(real, monkeypatch, out_json):
    if out_json is not None:
        monkeypatch.setenv("OUT_JSON", out_json)
    real.put()
    real.ctx.clock = at(13, 1, 31)
    real.run()
    assert len(real.ctx.spawned) == 1
    rid, _ = only_run(real)
    wait_exit(real, rid)
    before = real.state()
    res = real.run()
    after = real.state()
    assert len(res["errors"]) == 1 and rid in res["errors"][0]
    assert after["analyst"]["runs"][rid]["failed"] is True
    assert res["analyst_done"] == [{"run": rid, "mode": "daily", "failed": True, "error": res["errors"][0]}]
    rest_before, rest_after = copy.deepcopy(before), copy.deepcopy(after)
    for s in (rest_before, rest_after):
        s["analyst"]["runs"].pop(rid)
    assert rest_after == rest_before                                   # 别的状态一点没动
    assert set(after["analyst"]["runs"][rid]) - set(before["analyst"]["runs"][rid]) <= {"failed", "error", "failed_at"}
    res = real.run()
    assert len(real.ctx.spawned) == 1 and res["errors"] == []          # 当天不重起，也不再报
    assert "analyst_done" not in res


def test_daily_output_lands_verdicts_and_new_queue(ap):
    a1 = [{"n": 1, "direction": "测试方向", "at": "2026-11-12T00:00:00Z", "rollback": "关开关 x", "verdict": None}]
    ap.put(T1=biz(attempts=a1))
    ap.ctx.clock = at(13, 1, 31)
    ap.run()
    rid, _ = only_run(ap)
    finish(ap, rid, {"verdicts": [{"ticket": "T1", "attempt": 1, "verdict": "harmful", "touched": 3, "hit_wall": 3,
                                   "continued": 0, "note": "测试：护栏变坏"}], "walls": []})
    res = ap.run()
    st = ap.state()
    harm = [q for q in st["queue"] if q["kind"] == "harm"]
    assert len(harm) == 1 and res["new_queue"] == [harm[0]["id"]]     # store.push_queue 直推的项也进 new_queue
    assert res["rollback_needed"] == [{"ticket": "T1", "rollback": "关开关 x"}]
    assert st["tickets"]["T1"]["attempts"][0]["verdict"] == "harmful" and st["analyst"]["runs"][rid]["ingested"]
    res = ap.run()                                                     # 收过就不再收
    assert res["new_queue"] == [] and len(ap.state()["queue"]) == 1


def test_daily_item_errors_reported(ap):
    ap.put(T1=biz())
    ap.ctx.clock = at(13, 1, 31)
    ap.run()
    rid, _ = only_run(ap)
    finish(ap, rid, {"verdicts": [{"ticket": "T9", "attempt": 1, "verdict": "useful", "touched": 1, "hit_wall": 1,
                                   "continued": 1, "note": "n"}],
                     "walls": [{"ticket": "T1", "text": "测试新墙"}], "notes": "测试备注"})
    res = ap.run()
    st = ap.state()
    assert [w["text"] for w in st["tickets"]["T1"]["walls"]] == ["测试墙一", "测试新墙"]
    assert st["analyst"]["runs"][rid]["result"]["notes"] == "测试备注" and "analyst_done" not in res
    assert len(res["errors"]) == 1 and "T9" in res["errors"][0]
    assert st["analyst"]["runs"][rid]["ingested"] and not st["analyst"]["runs"][rid].get("failed")


def test_stale_run_times_out(ap, monkeypatch):
    stopped = []
    monkeypatch.setattr(launcher, "stop_now", stopped.append)
    ap.put()
    ap.ctx.clock = at(13, 1, 31)
    ap.run()
    rid, r = only_run(ap)
    ap.ctx.advance(119)
    res = ap.run()
    assert res["errors"] == [] and not runs(ap)[rid].get("failed") and stopped == []    # 还在跑：不动
    ap.ctx.advance(2)
    res = ap.run()
    assert runs(ap)[rid]["failed"] is True and len(res["errors"]) == 1 and rid in res["errors"][0]
    assert [x["run"] for x in res["analyst_done"] if x["failed"]] == [rid]
    assert stopped == [r["pid"]]
    ap.ctx.advance(60)
    assert ap.run()["errors"] == [] and len(ap.ctx.spawned) == 1


# ---- weekly ----------------------------------------------------------------------

def test_weekly_replaces_triage(ap):
    ap.put()
    weekly_dir(ap, summary={"release_sync": {"unlabeled": ["x"]}})
    res = ap.run()
    assert "triage_needed" not in res
    rid, r = only_run(ap)
    assert r["mode"] == "weekly" and len(ap.ctx.spawned) == 1
    assert ap.state()["analyst"]["last_weekly"] == "2026-11-09"
    assert res["releases_unlabeled"] == ["x"]
    res = ap.run()
    assert len(ap.ctx.spawned) == 1 and "releases_unlabeled" not in res      # 同一期只起一次、只转一次


def test_wake_keys():
    assert "triage_needed" not in tick.WAKE_KEYS
    for k in ("analyst_done", "releases_unlabeled", "rollback_needed", "no_entry", "return_to_user", "new_queue"):
        assert k in tick.WAKE_KEYS, k
    assert "triage_needed" not in tick._empty_res()


def test_weekly_output_becomes_proposals(real, monkeypatch):
    d = json.loads((FIX / "proposals.json").read_text())
    d.update(mode="weekly", problems=d["problems"][:1], maint=[], walls=[{"ticket": "T1", "text": "测试新墙"}])
    monkeypatch.setenv("OUT_JSON", json.dumps(d, ensure_ascii=False))
    s = make_state(T1=biz())
    s["analyst"] = {"calibrated": True}
    store.save_state(real.home / "state.json", s)
    weekly_dir(real)
    real.run()
    rid, _ = only_run(real)
    wait_exit(real, rid)
    res = real.run()
    st = real.state()
    vq = [q for q in st["queue"] if q["kind"] == "verify_problem"]
    assert len(vq) == 1 and res["new_queue"] == [vq[0]["id"]]
    assert st["analyst"]["runs"][rid]["ingested"] and len(st["tickets"]["T1"]["walls"]) == 2
    res = real.run()                                                   # 收过一次（重新读盘后）也不再收
    st = real.state()
    assert res["new_queue"] == [] and len(st["queue"]) == 1 and len(st["proposals"]) == 1
    assert len(st["tickets"]["T1"]["walls"]) == 2


def test_weekly_mode_comes_from_run_not_output(ap):
    """weekly 的产出自称 migrate 也绕不过校准门：模式以 run 记录为准。"""
    d = json.loads((FIX / "proposals.json").read_text())                # mode = migrate
    ap.put()
    weekly_dir(ap)
    ap.run()
    rid, _ = only_run(ap)
    before = ap.state()
    finish(ap, rid, d)
    res = ap.run()
    st = ap.state()
    assert len(res["errors"]) == 1 and "校准未过" in res["errors"][0]
    assert res["analyst_done"] == [{"run": rid, "mode": "weekly", "failed": True, "error": res["errors"][0]}]
    assert st["analyst"]["runs"][rid]["failed"] is True and not st.get("proposals") and st["queue"] == []
    assert st["tickets"] == before["tickets"]


def test_weekly_not_configured_still_forwards_unlabeled(proj):
    proj.put()
    proj.ctx.clock = at(13, 1, 31)
    weekly_dir(proj, summary={"release_sync": {"unlabeled": ["x"]}})
    res = proj.run()
    assert proj.ctx.spawned == [] and res["releases_unlabeled"] == ["x"]
    assert len(res["errors"]) == 1 and "analyst.brief" in res["errors"][0]
    assert res["analyst_done"] == [{"run": None, "mode": "weekly", "failed": True, "error": res["errors"][0]}]
    res = proj.run()
    assert res["errors"] == [] and proj.ctx.spawned == [] and "analyst_done" not in res
    with pytest.raises(analyst.AnalystError):
        analyst.start(SimpleNamespace(cfg=proj.cfg, home=proj.home, ctx=proj.ctx), proj.state(), "calibrate")


# ---- calibrate -------------------------------------------------------------------

def test_calibrate_done_wakes_router(ap):
    ap.put()
    st = ap.state()
    env = SimpleNamespace(cfg=ap.cfg, home=ap.home, ctx=ap.ctx)
    rid = analyst.start(env, st, "calibrate", "2026-10-30", "2026-11-13")
    assert analyst.start(env, st, "calibrate") == rid and len(ap.ctx.spawned) == 1    # 同模式还在跑：不起第二个
    store.save_state(ap.home / "state.json", st)
    finish(ap, rid, {"mode": "calibrate", "problems": []})
    res = ap.run()
    assert res["analyst_done"] == [{"run": rid, "mode": "calibrate", "out": runs(ap)[rid]["out"]}]
    st = ap.state()
    assert st["queue"] == [] and not st.get("proposals") and st["analyst"]["runs"][rid]["ingested"]
    assert "analyst_done" not in ap.run()


def test_migrate_done_not_auto_applied(ap):
    """9b round 2（controller 裁决）：migrate 产出不在 analyst_done 时自动落；只叫醒 router，等用户确认后 migrate-apply。"""
    ap.put(T5=("postlaunch", [], {"title": "老票五"}), T7=biz("业务七"))
    st = ap.state()
    env = SimpleNamespace(cfg=ap.cfg, home=ap.home, ctx=ap.ctx)
    rid = analyst.start(env, st, "migrate")
    store.save_state(ap.home / "state.json", st)
    d = json.loads((FIX / "proposals.json").read_text())
    d.update(maint=[], attach=[{"ticket": "T7", "absorbs": ["T5"]}])
    finish(ap, rid, d)
    res = ap.run()
    assert res["analyst_done"] == [{"run": rid, "mode": "migrate", "out": runs(ap)[rid]["out"]}]
    st = ap.state()
    assert st["queue"] == [] and not st.get("proposals") and st["tickets"]["T5"].get("parent") is None
    assert st["analyst"]["runs"][rid]["ingested"] and not st["analyst"]["runs"][rid].get("applied")
    assert "new_queue" not in res or res["new_queue"] == []


def test_migrate_bad_output_fails_once(ap):
    ap.put()
    st = ap.state()
    env = SimpleNamespace(cfg=ap.cfg, home=ap.home, ctx=ap.ctx)
    rid = analyst.start(env, st, "migrate")
    store.save_state(ap.home / "state.json", st)
    finish(ap, rid, {"mode": "migrate", "problems": [{"key": "k"}]})            # 缺字段：预检不过
    res = ap.run()
    assert [x["run"] for x in res["analyst_done"] if x.get("failed")] == [rid]
    assert ap.state()["analyst"]["runs"][rid]["failed"]


def test_state_summary_shows_current_not_historical(ap):
    """9b round 2：「最近一次尝试」= 最后一条非历史的尝试（历史尝试排在 live 之后也不能盖住它）。"""
    s = make_state(T1=biz("业务一", attempts=[
        {"n": 1, "direction": "测试方向甲", "at": "2026-11-10T00:00:00Z", "rollback": "r", "verdict": None},
        {"n": 2, "direction": "老票五", "shipped_at": "2026-11-01T00:00:00Z", "verdict": "legacy",
         "legacy_verdict": "没用", "historical": True}]))
    text = analyst.state_summary(s, "daily")
    assert "尝试 1「测试方向甲」" in text and "待判" in text and "尝试 2「老票五」" not in text


def test_stale_out_file_not_taken_as_new_output(ap):
    """state 重建后编号复用：同名旧产出要先清掉，不能冒充这一次的结果。"""
    out = ap.home / "analyst" / "out" / "calibrate-1.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps({"mode": "calibrate", "problems": []}))
    ap.put()
    st = ap.state()
    rid = analyst.start(SimpleNamespace(cfg=ap.cfg, home=ap.home, ctx=ap.ctx), st, "calibrate")
    assert rid == "calibrate-1" and not out.exists()


def test_failed_run_wakes_watch(ap, monkeypatch):
    """失败只进 errors 的话，watch 要连错 3 轮才醒：失败也发 analyst_done（failed=True）。"""
    ap.put()
    ap.ctx.clock = at(13, 1, 31)
    ap.run()
    rid, _ = only_run(ap)
    ap.ctx.alive.clear()                                               # 退出了，没写产出
    monkeypatch.setattr(tick.time, "sleep", lambda s: None)
    env = SimpleNamespace(ctx=ap.ctx, root=ap.root, cfg=ap.cfg)
    out = tick.cmd_watch(SimpleNamespace(interval=0, max_minutes=10), env)
    assert "analyst_done" in out["woke"]
    (item,) = out["analyst_done"]
    assert item["run"] == rid and item["mode"] == "daily" and item["failed"] is True and rid in item["error"]


def test_calibrate_spawns_blind(ap):
    """calibrate：cwd 是项目外的空临时目录，环境里没有 ROUTER_HOME；OUT / 日志仍是 router home 下的绝对路径。"""
    import shutil
    ap.put()
    st = ap.state()
    env = SimpleNamespace(cfg=ap.cfg, home=ap.home, ctx=ap.ctx)
    rid = analyst.start(env, st, "calibrate")
    sp = ap.ctx.spawned[0]
    cwd = Path(sp["cwd"])
    try:
        assert cwd.is_dir() and list(cwd.iterdir()) == [] and cwd.name.startswith("router-calibrate-")
        assert ap.root.resolve() not in cwd.resolve().parents and cwd.resolve() != ap.root.resolve()
        assert sp["env"].get("ROUTER_HOME") is None and sp["env"]["ROUTER_TICKET"] == "analyst"
        r = st["analyst"]["runs"][rid]
        assert Path(r["out"]).is_absolute() and Path(r["out"]).parent == ap.home / "analyst" / "out"
        assert Path(sp["log"]).is_absolute() and Path(sp["log"]).parent == ap.home / "analyst" / "logs"
        assert f"OUT={r['out']}" in sp["cmd"][sp["cmd"].index("-p") + 1]
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    rid2 = analyst.start(env, st, "migrate")                            # 其余模式照旧：cwd = router home
    assert ap.ctx.spawned[1]["cwd"] == str(ap.home) and ap.ctx.spawned[1]["env"]["ROUTER_HOME"] == str(ap.home)
    assert rid2 != rid


def test_calibrate_real_process_has_no_router_home(real, monkeypatch):
    """真进程：router 自己环境里有 ROUTER_HOME，calibrate 子进程里也必须没有。"""
    import shutil
    probe = real.tmp / "probe.json"
    hook = real.tmp / "probe_hook.py"
    hook.write_text("import json, os\n"
                    f"open({str(probe)!r}, 'w').write(json.dumps({{'cwd': os.getcwd(), "
                    "'home': os.environ.get('ROUTER_HOME'), 'ticket': os.environ.get('ROUTER_TICKET')}))\n")
    monkeypatch.setenv("FAKE_CLAUDE_HOOK", str(hook))
    monkeypatch.setenv("ROUTER_HOME", str(real.home))
    real.put()
    st = real.state()
    rid = analyst.start(SimpleNamespace(cfg=real.cfg, home=real.home, ctx=real.ctx), st, "calibrate")
    store.save_state(real.home / "state.json", st)
    wait_exit(real, rid)
    got = json.loads(probe.read_text())
    try:
        assert got["home"] is None and got["ticket"] == "analyst"
        assert real.root.resolve() not in Path(got["cwd"]).resolve().parents
        assert Path(got["cwd"]).resolve() == Path(st["analyst"]["runs"][rid]["cwd"]).resolve()
    finally:
        shutil.rmtree(st["analyst"]["runs"][rid]["cwd"], ignore_errors=True)


# ---- CLI -------------------------------------------------------------------------

@pytest.fixture
def cli(tmp_path, monkeypatch):
    root = tmp_path / "proj"; home = root / ".claude" / "router"; home.mkdir(parents=True)
    brief = tmp_path / "brief.md"; brief.write_text("合成 brief\n")
    cfg = {"repo": "o/r", "base_branch": "staging", "leak_guard_globs": [],
           "worker": {"model": "m", "permission_mode": "bypassPermissions", "allowed_tools": [],
                      "disallowed_tools": [], "claude_bin": [sys.executable, str(FAKE)]},
           "analyst": {"brief": str(brief)}}
    (home / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_LOG", str(tmp_path / "claude.log"))
    for k in ("ROUTER_HOME", "ROUTER_TICKET", "OUT_JSON", "FAKE_CLAUDE_HOOK"):
        monkeypatch.delenv(k, raising=False)
    log = tmp_path / "claude.log"

    def run(*args, env=None):
        r = subprocess.run([sys.executable, str(CLI), "--root", str(root), *args], capture_output=True, text=True,
                           env={**os.environ, **(env or {})}, timeout=30)
        return r.returncode, json.loads(r.stdout)
    return SimpleNamespace(run=run, home=home, cfg=cfg, root=root,
                           state=lambda: json.loads((home / "state.json").read_text()),
                           claude=lambda: [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else [])


def test_cli_analyst_run(cli):
    rc, out = cli.run("analyst", "run", "--mode", "calibrate", "--since", "2026-10-30", "--until", "2026-11-13")
    assert rc == 0 and out["ok"] and out["mode"] == "calibrate"
    r = cli.state()["analyst"]["runs"][out["run"]]
    assert r["out"] == out["out"] and r["since"] == "2026-10-30" and r["until"] == "2026-11-13"
    end = time.time() + 20
    while not cli.claude() and time.time() < end:
        time.sleep(0.05)
    argv = cli.claude()[0]
    prompt = argv[argv.index("-p") + 1]
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1" and f"OUT={r['out']}" in prompt
    rc, out = cli.run("analyst", "run", "--mode", "weekly", "--since", "11-01")
    assert rc == 1 and not out["ok"] and "--since" in out["error"]
    rc, out = cli.run("analyst", "run", "--mode", "weekly", env={"ROUTER_TICKET": "T1"})
    assert rc == 2 and not out["ok"]                                   # worker 不许起分析 agent


def test_cli_analyst_run_needs_brief(cli):
    cli.cfg["analyst"] = {}
    (cli.home / "config.json").write_text(json.dumps(cli.cfg))
    rc, out = cli.run("analyst", "run", "--mode", "calibrate")
    assert rc == 1 and "analyst.brief" in out["error"] and cli.claude() == []


def test_cli_analyst_calibrated(cli):
    rc, out = cli.run("analyst", "calibrated", "--pass", "--note", "测试：三个已知问题找到三个")
    an = cli.state()["analyst"]
    assert rc == 0 and out["ok"] and an["calibrated"] is True and an["calibrated_note"] == "测试：三个已知问题找到三个"
    rc, out = cli.run("analyst", "calibrated", "--fail", "--note", "测试：只找到一个")
    an = cli.state()["analyst"]
    assert rc == 0 and an["calibrated"] is False and an["calibrated_note"] == "测试：只找到一个"


def test_maint_start_failure_does_not_revert_ingest(ap, monkeypatch):
    """收产出成功后 start 维护票失败：提议 / 决定单项保留，维护票留 DRAFT，只报一条错；下一轮自动 start。"""
    import router
    d = json.loads((FIX / "proposals.json").read_text())
    d.update(mode="weekly", maint=[{"title": "甲", "problem": "p", "surfaces": ["llm-config"]}])
    s = make_state()
    s["analyst"] = {"calibrated": True}
    store.save_state(ap.home / "state.json", s)
    weekly_dir(ap)
    ap.run()
    rid, _ = only_run(ap)
    finish(ap, rid, d)

    def boom(env, st, sel):
        raise router.Fail("模拟 start 失败")
    monkeypatch.setattr(router, "_start_locked", boom)
    res = ap.run()
    st = ap.state()
    assert len(res["errors"]) == 1 and "DRAFT" in res["errors"][0]
    assert len(st["proposals"]) == 2 and len([q for q in st["queue"] if q["kind"] == "verify_problem"]) == 2
    assert st["analyst"]["runs"][rid]["ingested"] and "failed" not in st["analyst"]["runs"][rid]
    (mt,) = [t for t in st["tickets"].values() if t.get("kind") == "maint"]
    assert mt["status"] == "draft" and len(mt["tasks"]) == 1
    started = []
    monkeypatch.setattr(router, "_start_locked",
                        lambda env, st, sel: [started.append(t) or store.set_status(st, t, "queued", env.now()) for t in sel])
    res = ap.run()
    assert not res["errors"] and started == [mt["id"]]
    assert ap.state()["tickets"][mt["id"]]["status"] == "queued" and len(ap.state()["proposals"]) == 2
