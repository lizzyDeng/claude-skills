"""巡检常驻（daemon）：脱离 router session 的后台进程，每 interval 秒跑一轮 tick（派单、合 staging、收事件）。

之前 `watch` 身兼两职：巡检引擎 + 叫醒 router（退出 = 通知），每个事件都要 router 重启它，漏一次巡检就停。
现在拆开：巡检由这里常驻跑；叫醒 router 走事件流（feed.py：tick 把唤醒项写进 feed.jsonl，router 用 Monitor 挂
`router.py feed --follow`）。

  <home>/.daemon.lock   文件锁：拿到锁的那一份才跑（单实例；进程死了锁自动放）
  <home>/daemon.json    pid / 启动时间 / 到期时间 / 最后一轮时间 / 连续出错轮数（status 读它）
  <home>/logs/daemon.log

- 单轮出错（tick 抛异常、或结果里有 errors）不让 daemon 挂；连续 3 轮出错写一条 errors 事件（一段连续出错只写一次）。
- 默认 24 小时（--max-hours）到点自停，写一条 daemon_stopped 事件；stop 发 SIGTERM，跑完当前一轮再退。
- session 关掉之后照跑：派单、CI 全绿合 staging 都照常；上 prod、花钱不在巡检里（照旧归 router / 用户）。
"""
import fcntl
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import feed
import store

PIDFILE = "daemon.json"
LOCKFILE = ".daemon.lock"
LOG = "logs/daemon.log"
DEFAULT_INTERVAL = 60
DEFAULT_MAX_HOURS = 24
ERR_ROUNDS = 3
STALE_S = 180                   # 超过 3 分钟没巡检：看板标红、feed 报 daemon_down
SLICE_S = 1.0                   # 两轮之间分片睡：stop 最多晚 1 秒被看到
ROUTER_PY = Path(__file__).resolve().with_name("router.py")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def try_lock(home):
    """拿 daemon 锁（不等）→ 打开着的文件（拿着锁）；别人拿着 → None。"""
    Path(home).mkdir(parents=True, exist_ok=True)
    f = open(Path(home) / LOCKFILE, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def release(f) -> None:
    if f is None:
        return
    try:
        fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def running(home) -> bool:
    """有一份 daemon 拿着锁 ⇔ 在跑（不看 pid：pid 会被复用，锁随进程死自动放）。"""
    f = try_lock(home)
    if f is None:
        return True
    release(f)
    return False


def read_info(home) -> dict:
    p = Path(home) / PIDFILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_info(home, info) -> None:
    store._atomic_write(Path(home) / PIDFILE, json.dumps(info, ensure_ascii=False, indent=2) + "\n")


def last_patrol(home) -> dict:
    """最后一次巡检（任何调用方的 tick 都记）：state.json 的 patrol。读不了 → {}。"""
    try:
        st = json.loads((Path(home) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    p = st.get("patrol") if isinstance(st, dict) else None
    return p if isinstance(p, dict) else {}


def _age_s(at, now: datetime):
    try:
        return (now - store.parse_iso(at)).total_seconds()
    except (TypeError, ValueError):
        return None


def status(home, now: datetime) -> dict:
    on = running(home)
    info = read_info(home)                          # 没在跑时是上一份的记录（停在哪、为什么停）
    pat = last_patrol(home)
    age = _age_s(pat.get("at"), now)
    out = {"running": on, "pid": info.get("pid") if on else None, "started_at": info.get("started_at"),
           "deadline": info.get("deadline"), "interval": info.get("interval"), "rounds": info.get("rounds"),
           "last_round_at": info.get("last_round_at"), "err_streak": info.get("err_streak"),
           "last_patrol_at": pat.get("at"), "last_patrol_by": pat.get("by"),
           "stale": age is None or age > STALE_S, "log": str(Path(home) / LOG)}
    if not on and info:
        out.update(stopped_at=info.get("stopped_at"), stop_reason=info.get("stop_reason") or "crashed")
    return out


def down(home, now: datetime):
    """feed --follow 用：daemon 没在跑、且 3 分钟没人巡检 → 一句提醒；否则 None。"""
    if running(home):
        return None
    pat = last_patrol(home)
    age = _age_s(pat.get("at"), now)
    if age is not None and age <= STALE_S:
        return None
    last = f"最后一次巡检 {pat.get('at')}（{pat.get('by')}）" if pat.get("at") else "还没巡检过"
    return {"summary": f"巡检 daemon 没在跑，{last}：派单、合 staging、收事件都停了。router.py daemon start"}


# ---- 常驻循环 ---------------------------------------------------------------------------------------

def run_loop(home, *, interval=DEFAULT_INTERVAL, max_hours=DEFAULT_MAX_HOURS, make_env, clock=time.time, sleep=None,
             stop=None, max_rounds=None) -> dict:
    """拿到锁才跑；每轮新建 env（config 改了下一轮就生效）跑 tick.run。make_env() → 有 ctx / root / cfg 的对象。
    stop：threading.Event（SIGTERM 置位）；sleep / clock 可注入（测试不真睡）。"""
    import tick                                     # 运行时取：测试可以替换 tick.run
    home = Path(home)
    lock = try_lock(home)
    if lock is None:
        info = read_info(home)
        return {"ok": False, "error": f"已有 daemon 在跑（pid {info.get('pid')}，{info.get('started_at')} 起）：不起第二份",
                "pid": info.get("pid")}
    stop = stop or threading.Event()
    sleep = sleep or (lambda s: stop.wait(s))
    started = clock()
    deadline = started + max_hours * 3600
    info = {"pid": os.getpid(), "pid_start": None, "started_at": _iso(started), "deadline": _iso(deadline),
            "interval": interval, "max_hours": max_hours, "rounds": 0, "last_round_at": None, "last_errors": 0,
            "err_streak": 0, "log": str(home / LOG)}
    streak, streak_errs, streak_at, reason = 0, [], None, "stopped"
    try:
        try:
            info["pid_start"] = make_env().ctx.proc_start(os.getpid())
        except Exception:  # noqa: BLE001 — 查不到启动时间：stop 只凭锁 + pid
            pass
        _write_info(home, info)
        while not stop.is_set():
            t0 = clock()
            try:
                env = make_env()
                res = tick.run(env.ctx, env.root, env.cfg, resurface=info["rounds"] == 0, by="daemon")
                errs = [str(e) for e in (res or {}).get("errors") or []]
            except Exception as e:  # noqa: BLE001 — 一轮出错不能把 daemon 弄挂
                errs = [f"巡检异常：{e!r}"]
            info["rounds"] += 1
            info["last_round_at"], info["last_errors"] = _iso(clock()), len(errs)
            if errs:
                streak += 1
                streak_at = streak_at or _iso(t0)
                streak_errs = (streak_errs + [[x[:300] for x in errs[:5]]])[-ERR_ROUNDS:]
                if streak == ERR_ROUNDS:            # 一段连续出错只写一次；断了再连续 3 轮才写下一条
                    _event(home, f"errors:{streak_at}", "errors",
                           {"rounds": ERR_ROUNDS, "since": streak_at, "errors": streak_errs,
                            "summary": f"巡检连续 {ERR_ROUNDS} 轮报错（{streak_at} 起）"}, clock())
            else:
                streak, streak_errs, streak_at = 0, [], None
            info["err_streak"] = streak
            _write_info(home, info)
            if clock() >= deadline:
                reason = "max_hours"
                _event(home, f"daemon_stopped:{info['started_at']}", "daemon_stopped",
                       {"reason": "max_hours", "started_at": info["started_at"], "rounds": info["rounds"],
                        "summary": f"巡检 daemon 到了 {max_hours} 小时自停：要接着巡检就 router.py daemon start"}, clock())
                break
            if max_rounds and info["rounds"] >= max_rounds:
                reason = "max_rounds"
                break
            target = t0 + interval
            while not stop.is_set():
                left = target - clock()
                if left <= 0:
                    break
                sleep(min(left, SLICE_S))
    finally:
        info.update(stopped_at=_iso(clock()), stop_reason=reason if reason != "stopped" or stop.is_set() else "crashed")
        try:                                        # 留着最后一次的记录（status 看得到停在哪、为什么停）
            _write_info(home, info)
        except OSError:
            pass
        release(lock)
    return {"ok": True, "reason": reason, "rounds": info["rounds"], "started_at": info["started_at"]}


def _event(home, key, kind, item, ts) -> None:
    try:
        feed.append(home, key, kind, item, _iso(ts))
    except Exception:  # noqa: BLE001 — 写不了事件也不挂；下一轮照跑
        pass


def serve(env, interval, max_hours) -> dict:
    """`daemon run`：前台跑循环（daemon start 起的就是它）。SIGTERM / SIGINT ⇒ 跑完当前一轮退出。"""
    from ctx import Ctx
    import router
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    root = env.root
    return run_loop(env.home, interval=interval, max_hours=max_hours, make_env=lambda: router.Env(root, Ctx()),
                    stop=stop, sleep=lambda s: time.sleep(s))


# ---- start / stop -------------------------------------------------------------------------------

def start(env, interval=DEFAULT_INTERVAL, max_hours=DEFAULT_MAX_HOURS, wait_s=5.0, sleep=time.sleep) -> dict:
    """已在跑 ⇒ 不起第二份，报它的信息。没在跑 ⇒ 脱离 session 起一份（start_new_session，经 ctx.spawn_detached），
    最多等 wait_s 秒确认它拿到了锁。"""
    home = Path(env.home)
    if running(home):
        return {"ok": True, "already_running": True, **status(home, env.ctx.now())}
    log = home / LOG
    cmd = [sys.executable, str(ROUTER_PY), "--root", str(env.root), "daemon", "run",
           "--interval", f"{interval:g}", "--max-hours", f"{max_hours:g}"]
    pid = env.ctx.spawn_detached(cmd, env.root, log, env={"ROUTER_HOME": str(home)})
    waited = 0.0
    while waited < wait_s and not running(home):
        if not env.ctx.pid_alive(pid):
            break
        sleep(0.1)
        waited += 0.1
    up = running(home)
    if wait_s > 0 and not up:
        return {"ok": False, "error": f"daemon（pid {pid}）没起来或起来就退出了：看 {log}", "pid": pid, "log": str(log)}
    return {"ok": True, "started": True, "pid": pid, "confirmed": up, "log": str(log),
            "interval": interval, "max_hours": max_hours}


def stop(home, ctx, wait_s=120.0, force=False, kill=os.kill, sleep=time.sleep) -> dict:
    """发 SIGTERM，等它跑完当前一轮、放锁（最多 wait_s 秒）。认不出 pid（启动时间对不上）不发信号。
    --force：等不到就 SIGKILL（锁随进程死自动放）。"""
    home = Path(home)
    if not running(home):
        return {"ok": True, "stopped": False, "running": False, "note": "没在跑"}
    info = read_info(home)
    pid = info.get("pid")
    if not pid:
        return {"ok": False, "error": f"daemon 拿着锁但 {PIDFILE} 里没有 pid：手动查 {home / LOCKFILE} 的持有进程"}
    if info.get("pid_start") and ctx.proc_start(pid) not in (None, info["pid_start"]):
        return {"ok": False, "error": f"pid {pid} 的启动时间对不上（被复用了？）：不发信号"}
    kill(pid, signal.SIGTERM)
    waited = 0.0
    while waited < wait_s and running(home):
        sleep(0.1)
        waited += 0.1
    if running(home) and force:
        kill(pid, signal.SIGKILL)
        sleep(0.5)
    if running(home):
        return {"ok": False, "stopped": False, "running": True, "pid": pid,
                "error": f"发了 SIGTERM，{wait_s:.0f} 秒内还没停（可能在跑一轮长的巡检）：稍后 daemon status，或 --force"}
    return {"ok": True, "stopped": True, "pid": pid}
