"""全部 IO 出口：子进程、时钟、进程存活、环境变量。测试用 tests/router/conftest.py 的 FakeCtx 替换。"""
import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# router session 自己的会话绑定变量。worker 是独立会话，继承这些会被当成 router 的子会话。
SESSION_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
               "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
               "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID")


class Ctx:
    def run(self, cmd, cwd=None) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def spawn_detached(self, cmd, cwd, log: Path, env=None) -> int:
        """起一个脱离 router 进程组的子进程：router 退出、被压缩、Mac 合盖后 worker 照跑。"""
        full_env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV}
        full_env.update(env or {})
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as out:
            p = subprocess.Popen(cmd, cwd=str(cwd), stdout=out, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True, env=full_env)
        return p.pid

    def pid_alive(self, pid) -> bool:
        if not pid:
            return False
        try:                                   # 自己的子进程退出后是僵尸，kill(0) 仍成功 → 先收尸
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                return False
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def proc_start(self, pid):
        """进程启动时间（ps lstart）。pid 被复用时它会变：用来确认还是不是我们起的那个 worker。"""
        if not pid:
            return None
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        return r.stdout.strip() or None

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def post_json(self, url, payload, timeout=30) -> dict:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")

    def env(self, key):
        return os.environ.get(key)
