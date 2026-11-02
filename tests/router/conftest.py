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
        self.spawned = []
        self.alive = set()
        self.clock = now
        self.envs = {}

    def script(self, prefix, stdout="", returncode=0, stderr=""):
        self.scripted.append((list(prefix), stdout, returncode, stderr))
        return self

    def run(self, cmd, cwd=None):
        self.calls.append(list(cmd))
        best = None
        for prefix, out, rc, err in self.scripted:
            if cmd[:len(prefix)] == prefix and (best is None or len(prefix) >= len(best[0])):
                best = (prefix, out, rc, err)
        out, rc, err = (best[1], best[2], best[3]) if best else ("", 0, "")
        if callable(out):
            out = out(cmd)
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def spawn_detached(self, cmd, cwd, log, env=None):
        pid = 10000 + len(self.spawned)
        self.spawned.append({"cmd": list(cmd), "cwd": str(cwd), "log": str(log), "env": dict(env or {}), "pid": pid})
        self.alive.add(pid)
        return pid

    def pid_alive(self, pid):
        return pid in self.alive

    def kill(self, pid):
        self.alive.discard(pid)

    def now(self):
        return self.clock

    def advance(self, minutes):
        self.clock += timedelta(minutes=minutes)

    def env(self, key):
        return self.envs.get(key)

    def calls_with(self, *words):
        return [c for c in self.calls if all(w in c for w in words)]

    def last_call(self):
        return self.calls[-1] if self.calls else None


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
