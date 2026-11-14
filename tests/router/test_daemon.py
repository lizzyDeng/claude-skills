"""缺口 2：巡检常驻（daemon）+ 事件流（feed / ack）。

不起长期进程：daemon 循环在本进程里跑（短 interval、可注入的 clock / sleep / stop），唯一的子进程用例跑完即停。
夹具全是合成值；不调真 gh（FakeCtx）。
"""
import argparse
import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from conftest import SCRIPTS
from test_walls import UID, biz, seed  # noqa: F401  (biz 是 fixture)

import board
import daemon
import feed
import router
import store
import tick

CLI = SCRIPTS / "router.py"


class Clock:
    """daemon 循环用的假时钟（epoch 秒）：sleep 只推进时间，不真睡。"""

    def __init__(self, t=1_800_000_000.0):
        self.t = t
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += max(s, 0.001)


def loop(biz, clock=None, **kw):
    clock = clock or Clock()
    env = lambda: SimpleNamespace(ctx=biz.ctx, root=biz.root, cfg=biz.cfg, home=biz.home)
    args = dict(interval=60, max_hours=24, make_env=env, clock=clock, sleep=getattr(clock, "sleep", None))
    args.update(kw)
    return daemon.run_loop(biz.home, **args)


def lines(text):
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def _wake(biz, **wake):
    s = biz.state()
    s["wake"] = wake
    biz.save(s)


# ---- 事件流：tick 的唤醒键逐条进 feed，稳定编号、去重 ----------------------------------------------

def test_tick_writes_wake_keys_to_feed_once(biz):
    seed(biz)
    _wake(biz, rollback_needed=[{"ticket": "T1", "rollback": "关合成开关", "attempt": 2}],
          analyst_done=[{"run": "daily-3", "mode": "daily", "failed": True, "error": "合成错误"}])
    res = biz.tick()
    assert res["rollback_needed"] and res["analyst_done"]
    evs = feed.read_events(biz.home)
    assert [e["kind"] for e in evs] == ["rollback_needed", "analyst_done"]
    assert [e["id"] for e in evs] == ["E1", "E2"] and evs[0]["item"]["rollback"] == "关合成开关"
    _wake(biz, rollback_needed=[{"ticket": "T1", "rollback": "关合成开关", "attempt": 2}])   # 同一件事再交一次
    biz.tick()
    assert len(feed.read_events(biz.home)) == 2                                          # 自然 id 去重：不重复写


def test_redelivered_keys_written_once_per_natural_id(biz):
    s = seed(biz)
    s["tickets"]["T1"].update(kind=None, postlaunch=store.postlaunch_plan("2026-11-01T10:00:00Z"))
    biz.save(s)
    for _ in range(3):                                                                   # 每一轮都交 postlaunch_due
        assert biz.tick()["postlaunch_due"]
    evs = [e for e in feed.read_events(biz.home) if e["kind"] == "postlaunch_due"]
    assert len(evs) == 1 and evs[0]["item"] == {"ticket": "T1", "check": "T+1"}


def test_new_queue_event_carries_queue_item(biz):
    seed(biz, walls=[f"带 {UID} 的墙"])                          # issue 同步泄漏 ⇒ 进决定单
    biz.tick()
    (ev,) = [e for e in feed.read_events(biz.home) if e["kind"] == "new_queue"]
    q = biz.state()["queue"][0]
    assert ev["item"]["qid"] == q["id"] and ev["item"]["summary"] == q["summary"] and ev["key"] == f"new_queue:{q['id']}"


# ---- feed / ack ----------------------------------------------------------------------------------

def _put(biz, n=3):
    for i in range(1, n + 1):
        feed.append(biz.home, f"new_queue:Q{i}", "new_queue", {"qid": f"Q{i}", "summary": f"合成项 {i}"}, "2026-11-14T00:00:00Z")


def test_feed_only_unacked_and_replays_on_restart(biz):
    _put(biz)
    out = []
    feed.follow(biz.home, out.append, clock=Clock(), sleep=lambda s: None, max_s=0)
    assert [x["id"] for x in map(json.loads, out[:-1])] == ["E1", "E2", "E3"]
    assert json.loads(out[-1])["kind"] == "feed_end"
    router.cmd_ack(argparse.Namespace(ids=["E2"], all=False), biz.env())
    out = []
    feed.follow(biz.home, out.append, clock=Clock(), sleep=lambda s: None, max_s=0)       # 重开：补发全部未 ack 的
    assert [x["id"] for x in map(json.loads, out[:-1])] == ["E1", "E3"]


def test_feed_follow_streams_new_events_and_reminds_once_after_30_min(biz):
    _put(biz, 1)
    clock, out = Clock(), []

    def sleep(s):
        clock.sleep(s)
        if len(clock.slept) == 1:                                                         # 跟着的时候来了一条新的
            feed.append(biz.home, "new_queue:Q9", "new_queue", {"qid": "Q9"}, "2026-11-14T00:01:00Z")
        if len(clock.slept) == 2:
            feed.ack(biz.home, ["E2"], "2026-11-14T00:02:00Z")
    feed.follow(biz.home, out.append, clock=clock, sleep=sleep, interval=60, max_s=95 * 60)
    got = [json.loads(x) for x in out]
    e1 = [g for g in got if g.get("id") == "E1"]
    assert len(e1) == 2 and not e1[0].get("remind") and e1[1]["remind"] is True       # 30 分钟没 ack：再提醒一次（只一次）
    assert len([g for g in got if g.get("id") == "E2"]) == 1                            # ack 了就不再提醒
    assert got[-1]["kind"] == "feed_end"


def test_feed_cli_lines_are_flushed_json(biz, tmp_path):
    _put(biz, 2)
    r = subprocess.run([sys.executable, str(CLI), "--root", str(biz.root), "feed"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, timeout=60)
    got = lines(r.stdout)
    assert r.returncode == 0 and [g.get("id") for g in got[:2]] == ["E1", "E2"] and got[-1]["ok"] is True


def test_ack_idempotent_and_unknown_reported(biz):
    _put(biz, 2)
    out = router.cmd_ack(argparse.Namespace(ids=["E1"], all=False), biz.env())
    assert out["acked"] == ["E1"] and out["already"] == []
    out = router.cmd_ack(argparse.Namespace(ids=["E1", "E2"], all=False), biz.env())
    assert out["acked"] == ["E2"] and out["already"] == ["E1"]
    assert [e["id"] for e in feed.unacked(biz.home)] == []
    with pytest.raises(router.Fail) as e:
        router.cmd_ack(argparse.Namespace(ids=["E99"], all=False), biz.env())
    assert "E99" in str(e.value)
    _put(biz, 1)                                                                         # key 已存在：不重复写
    assert len(feed.read_events(biz.home)) == 2


def test_ack_slow_compat_acks_slow_events_and_ack_prunes_state(biz):
    seed(biz)
    ev = {"ts": "2026-11-14T00:00:00Z", "ticket": "T1", "type": "finding", "summary": "合成发现", "evidence": "/x"}
    s = biz.state(); s["slow_path"] = [ev]; biz.save(s)
    feed.append(biz.home, feed.slow_key(ev), "slow_path_events", ev, "2026-11-14T00:00:00Z")
    out = router.cmd_ack_slow(argparse.Namespace(), biz.env())
    assert out["cleared"] == 1 and feed.unacked(biz.home) == []
    s = biz.state(); s["slow_path"] = [ev]; biz.save(s)                                   # ack 了的慢路径：下一轮从 state 清掉
    biz.tick()
    assert biz.state()["slow_path"] == []


# ---- daemon：单实例、心跳、异常不挂、max-hours 自停、stop / status --------------------------------

def test_daemon_single_instance_lock(biz):
    held = daemon.try_lock(biz.home)
    assert held is not None and daemon.running(biz.home)
    out = loop(biz, max_rounds=1)
    assert out["ok"] is False and "已有" in out["error"]
    r = subprocess.run([sys.executable, str(CLI), "--root", str(biz.root), "daemon", "run", "--interval", "1"],
                       capture_output=True, text=True, timeout=60)                       # 真子进程：拿不到锁立刻退出
    assert r.returncode == 1 and "已有" in json.loads(r.stdout)["error"]
    daemon.release(held)
    assert not daemon.running(biz.home)


def test_daemon_heartbeat_and_board(biz):
    seed(biz)
    out = loop(biz, max_rounds=2)
    assert out["ok"] and out["rounds"] == 2 and out["reason"] == "max_rounds"
    info = daemon.read_info(biz.home)                                                    # 停了也留着最后一次的记录
    assert info["rounds"] == 2 and info["last_round_at"] and info["stop_reason"] == "max_rounds" and info["stopped_at"]
    st = biz.state()
    assert st["patrol"]["by"] == "daemon" and st["patrol"]["at"]
    html = (biz.home / "board.html").read_text()
    assert "最后一次巡检" in html and "patrol-stale" not in html.split("<body>")[1].split("<script>")[0]
    assert not daemon.running(biz.home)                                                  # 退出时放锁
    st = daemon.status(biz.home, datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc))
    assert st["running"] is False and st["pid"] is None and st["stop_reason"] == "max_rounds"


def test_board_marks_stale_patrol_red():
    st = store.empty_state()
    now = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
    st["patrol"] = {"at": "2026-11-14T09:56:00Z", "by": "daemon"}
    html = board.render(st, [], {}, now)
    assert 'class="patrol patrol-stale"' in html and "data-patrol-at=\"2026-11-14T09:56:00Z\"" in html
    st["patrol"]["at"] = "2026-11-14T09:59:00Z"
    assert 'class="patrol patrol-stale"' not in board.render(st, [], {}, now)
    assert 'class="patrol patrol-stale"' in board.render(store.empty_state(), [], {}, now)   # 从没巡检过也标红


def test_daemon_survives_tick_exceptions_and_reports_after_three(biz, monkeypatch):
    calls = []

    def boom(*a, **kw):
        calls.append(1)
        raise RuntimeError("合成：巡检炸了")
    monkeypatch.setattr(tick, "run", boom)
    out = loop(biz, max_rounds=5)
    assert out["ok"] and len(calls) == 5                                                  # 一轮异常不把 daemon 弄挂
    errs = [e for e in feed.read_events(biz.home) if e["kind"] == "errors"]
    assert len(errs) == 1 and errs[0]["item"]["rounds"] == 3 and "巡检炸了" in json.dumps(errs[0], ensure_ascii=False)


def test_daemon_errors_in_result_count_and_streak_resets(biz, monkeypatch):
    seq = iter([["e1"], ["e2"], [], ["e3"], ["e4"], ["e5"], ["e6"]])
    monkeypatch.setattr(tick, "run", lambda *a, **kw: {"errors": next(seq)})
    loop(biz, max_rounds=7)
    errs = [e for e in feed.read_events(biz.home) if e["kind"] == "errors"]
    assert len(errs) == 1 and errs[0]["item"]["errors"] == [["e3"], ["e4"], ["e5"]]       # 断过一次：从断后重新数


def test_daemon_max_hours_self_stop(biz, monkeypatch):
    monkeypatch.setattr(tick, "run", lambda *a, **kw: {"errors": []})
    clock = Clock()
    out = loop(biz, clock=clock, interval=3600, max_hours=2)
    assert out["reason"] == "max_hours" and out["rounds"] == 3                            # 0h / 1h / 2h 各一轮，到点自停
    (ev,) = [e for e in feed.read_events(biz.home) if e["kind"] == "daemon_stopped"]
    assert ev["item"]["reason"] == "max_hours"
    assert not daemon.running(biz.home)


def test_daemon_stop_and_status(biz, monkeypatch):
    monkeypatch.setattr(tick, "run", lambda *a, **kw: {"errors": []})
    stop = threading.Event()
    started = threading.Event()

    def sleep(s):
        started.set()
        stop.wait(0.05)
    th = threading.Thread(target=loop, args=(biz,), kwargs=dict(clock=__import__("time").time, sleep=sleep,
                                                                    interval=0.01, stop=stop))
    th.start()
    assert started.wait(10)
    st = daemon.status(biz.home, datetime.now(timezone.utc))
    assert st["running"] is True and st["pid"] and st["last_round_at"]
    sent = []
    out = daemon.stop(biz.home, biz.ctx, wait_s=10, kill=lambda pid, sig: (sent.append((pid, sig)), stop.set()))
    th.join(10)
    assert out["ok"] and out["stopped"] is True and sent and not th.is_alive()
    assert daemon.status(biz.home, datetime.now(timezone.utc))["running"] is False
    assert daemon.stop(biz.home, biz.ctx, wait_s=0)["stopped"] is False                  # 没在跑：干净返回


def test_daemon_start_spawns_detached_once(biz):
    env = biz.env()
    out = daemon.start(env, interval=60, max_hours=24, wait_s=0)
    (sp,) = biz.ctx.spawned
    assert out["ok"] and sp["cmd"][-6:] == ["daemon", "run", "--interval", "60", "--max-hours", "24"]
    held = daemon.try_lock(biz.home)                                                     # 模拟它已经起来、拿着锁
    out = daemon.start(env, interval=60, max_hours=24, wait_s=0)
    assert out["already_running"] is True and len(biz.ctx.spawned) == 1
    daemon.release(held)


def test_watch_refuses_while_daemon_running(biz, monkeypatch):
    seed(biz)
    held = daemon.try_lock(biz.home)
    ran = []
    monkeypatch.setattr(tick, "run", lambda *a, **kw: ran.append(1) or {"errors": []})
    env = SimpleNamespace(ctx=biz.ctx, root=biz.root, cfg=biz.cfg)
    with pytest.raises(router.Fail) as e:
        tick.cmd_watch(SimpleNamespace(interval=0, max_minutes=0), env)
    assert "daemon" in str(e.value) and "feed" in str(e.value) and ran == []              # 不并行 tick
    daemon.release(held)


def test_feed_warns_once_when_daemon_down(biz):
    s = seed(biz)
    s["patrol"] = {"at": "2026-11-14T00:00:00Z", "by": "daemon"}
    biz.save(s)
    out = []
    now = datetime(2026, 11, 14, 1, 0, tzinfo=timezone.utc)
    check = lambda: daemon.down(biz.home, now)
    clock = Clock()
    feed.follow(biz.home, out.append, clock=clock, sleep=clock.sleep, interval=40, max_s=200, daemon_check=check)
    downs = [json.loads(x) for x in out if json.loads(x).get("kind") == "daemon_down"]
    assert len(downs) == 1 and "daemon start" in downs[0]["summary"]
    held = daemon.try_lock(biz.home)
    assert daemon.down(biz.home, now) is None
    daemon.release(held)
