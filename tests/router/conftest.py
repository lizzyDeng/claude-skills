import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "router" / "scripts"
sys.path.insert(0, str(SCRIPTS))   # 脚本之间是平铺 import（import store），必须先上 path
FIX = Path(__file__).resolve().parent / "fixtures"


class FakeCtx:
    """与 ctx.Ctx 同一组方法。run 按命令前缀查脚本化应答（最长前缀优先），未脚本化的返回空成功。"""

    def __init__(self, now=datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)):
        self.scripted = []
        self.calls = []
        self.timeouts = []           # 与 calls 一一对应：每次 run 传进来的 timeout（没传就是 None）
        self.spawned = []
        self.spawn_attempts = []     # 每次 spawn_detached 调用（含抛错的那次）
        self.spawn_error = None      # 设成异常实例：spawn_detached 抛它（模拟 Popen 失败，子进程没起）
        self.alive = set()
        self.clock = now
        self.envs = {}
        self.post_attempts = []      # 每次 post_json 调用（含抛错的那次）
        self.post_errors = []        # 依次消费：元素是异常实例就抛，None 就照常成功

    def script(self, prefix, stdout="", returncode=0, stderr=""):
        """stdout 可以是 callable(cmd)：返回字符串，或 (stdout, returncode, stderr) 三元组；
        returncode 也可以是 callable(cmd)。最长前缀优先，同长后写的覆盖先写的。"""
        self.scripted.append((list(prefix), stdout, returncode, stderr))
        return self

    def run(self, cmd, cwd=None, timeout=None):
        self.calls.append(list(cmd))
        self.timeouts.append(timeout)
        best = None
        for prefix, out, rc, err in self.scripted:
            if cmd[:len(prefix)] == prefix and (best is None or len(prefix) >= len(best[0])):
                best = (prefix, out, rc, err)
        out, rc, err = (best[1], best[2], best[3]) if best else ("", 0, "")
        if callable(out):
            out = out(cmd)
            if isinstance(out, tuple):
                out, rc, err = out
        if callable(rc):
            rc = rc(cmd)
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def timeout_of(self, *words):
        """第一条包含这些词的命令当时传的 timeout。"""
        for c, t in zip(self.calls, self.timeouts):
            if all(w in c for w in words):
                return t
        raise AssertionError(f"没有包含 {words} 的命令")

    def spawn_detached(self, cmd, cwd, log, env=None):
        self.spawn_attempts.append({"cmd": list(cmd), "cwd": str(cwd), "log": str(log), "env": dict(env or {})})
        if self.spawn_error is not None:
            raise self.spawn_error
        pid = 10000 + len(self.spawned)
        self.spawned.append({"cmd": list(cmd), "cwd": str(cwd), "log": str(log), "env": dict(env or {}), "pid": pid})
        self.alive.add(pid)
        return pid

    def pid_alive(self, pid):
        return pid in self.alive

    starts = None

    def proc_start(self, pid):
        if pid not in self.alive:
            return None
        return (self.starts or {}).get(pid, f"start-{pid}")

    def kill(self, pid):
        self.alive.discard(pid)

    def now(self):
        return self.clock

    def advance(self, minutes):
        self.clock += timedelta(minutes=minutes)

    def env(self, key):
        return self.envs.get(key)

    posted = None

    def post_json(self, url, payload, timeout=10):
        self.post_attempts.append((url, payload, timeout))
        if self.post_errors:
            err = self.post_errors.pop(0)
            if err is not None:
                raise err
        self.posted = (self.posted or []) + [(url, payload)]
        return {"code": 0}

    def calls_with(self, *words):
        return [c for c in self.calls if all(w in c for w in words)]

    def last_call(self):
        return self.calls[-1] if self.calls else None


class RealProcCtx(FakeCtx):
    """git / gh 照旧脚本化，但起进程、查存活、查启动时间用真的 ctx.Ctx：
    用来验证「派单恢复不靠 argv、只靠启动回执 + worker_alive」（H4）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        import ctx as real_ctx
        self._real = real_ctx.Ctx()

    def spawn_detached(self, cmd, cwd, log, env=None):
        self.spawn_attempts.append({"cmd": list(cmd), "cwd": str(cwd), "log": str(log), "env": dict(env or {})})
        pid = self._real.spawn_detached(cmd, cwd, log, env=env)
        self.spawned.append({"cmd": list(cmd), "cwd": str(cwd), "log": str(log), "env": dict(env or {}), "pid": pid})
        return pid

    def pid_alive(self, pid):
        return self._real.pid_alive(pid)

    def proc_start(self, pid):
        return self._real.proc_start(pid)

    def kill(self, pid):
        import os
        import signal
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@pytest.fixture
def fake():
    return FakeCtx()


@pytest.fixture
def table():
    import surfaces
    return surfaces.load_surfaces(FIX / "surfaces.json")


def make_state(external=None, **tickets):
    """tickets: T1=(status, surfaces) 或 (status, surfaces, {其它字段})；external: path → surfaces 列表或完整 dict。"""
    import store
    s = store.empty_state()
    for tid, spec in tickets.items():
        status, surf, *rest = spec
        t = store._ticket_defaults()
        t.update(id=tid, title=tid, status=status, surfaces=list(surf))
        for k, v in (rest[0] if rest else {}).items():
            t[k] = {**t[k], **v} if isinstance(t.get(k), dict) and isinstance(v, dict) else v
        s["tickets"][tid] = t
        s["seq"]["ticket"] = max(s["seq"]["ticket"], int(tid[1:]))
    for path, ext in (external or {}).items():
        s["external"][path] = ext if isinstance(ext, dict) else {
            "surfaces": list(ext), "weight": "light", "active": True, "locking": True}
    return s


@pytest.fixture
def state_with():
    return make_state
