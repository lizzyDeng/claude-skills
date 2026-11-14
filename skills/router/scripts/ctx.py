"""全部 IO 出口：子进程、时钟、进程存活、环境变量。测试用 tests/router/conftest.py 的 FakeCtx 替换。"""
import json
import os
import select
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# router session 自己的会话绑定变量。worker 是独立会话，继承这些会被当成 router 的子会话。
SESSION_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
               "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
               "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID")


TIMEOUT_RC = 124                 # 超时的返回码（同 coreutils timeout）：调用方一律归入 unknown，绝不当成「不存在」
EXEC_WRAPPER = Path(__file__).resolve().with_name("exec_receipt.py")
EXEC_CONFIRM_S = 30              # 等 wrapper 确认 exec 的上限；超了按「已起」处理（宁可少派，不重起）


def _read_until_eof(fd, timeout) -> bytes:
    end, buf = time.monotonic() + timeout, b""
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return buf
        ready, _, _ = select.select([fd], [], [], left)
        if not ready:
            return buf
        chunk = os.read(fd, 4096)
        if not chunk:
            return buf
        buf += chunk


class Ctx:
    def run(self, cmd, cwd=None, timeout=600) -> subprocess.CompletedProcess:
        """默认 600 秒上界：弱网 / 挂死的子进程不能拖住整轮巡检。超时返回 rc=124（不抛）。
        GIT_TERMINAL_PROMPT=0：git 要凭据时直接失败，不在后台等输入。"""
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                  timeout=timeout, env=env)
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return subprocess.CompletedProcess(cmd, TIMEOUT_RC, out, f"timeout：{timeout} 秒没结束（{' '.join(map(str, cmd[:3]))}）")

    def spawn_detached(self, cmd, cwd, log: Path, env=None) -> int:
        """起一个脱离 router 进程组的子进程：router 退出、被压缩、Mac 合盖后 worker 照跑。

        经 exec_receipt.py 起：它在 exec 真正的命令之前，按需（env 里有 ROUTER_RECEIPT）原子写启动回执，
        再通过一条 close-on-exec 管道告诉这里 exec 成没成。语义与 Popen 一致：
        抛异常 ⇔ 子进程没起（找不到可执行文件、回执写不进去……）；返回 pid ⇔ 已经 exec。
        env 里值为 None 的键 = 从继承的环境里去掉（分析 agent 的 calibrate 不许知道 ROUTER_HOME）。"""
        full_env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV}
        for k, v in (env or {}).items():
            if v is None:
                full_env.pop(k, None)
            else:
                full_env[k] = v
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        r, w = os.pipe()
        try:
            full_env["ROUTER_EXEC_FD"] = str(w)
            with open(log, "ab") as out:
                p = subprocess.Popen([sys.executable, "-I", str(EXEC_WRAPPER), "--", *map(str, cmd)], cwd=str(cwd),
                                     stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     start_new_session=True, env=full_env, pass_fds=(w,))
        except BaseException:
            os.close(r)
            raise
        finally:
            os.close(w)
        try:
            report = _read_until_eof(r, EXEC_CONFIRM_S)
        finally:
            os.close(r)
        if report:                                   # wrapper 没能 exec：子进程已自行退出
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            try:
                e = json.loads(report.decode(errors="replace"))
            except ValueError:
                e = {"errno": 0, "strerror": report.decode(errors="replace")[:300]}
            raise OSError(e.get("errno") or 0, e.get("strerror") or "exec 失败", *([e["filename"]] if e.get("filename") else []))
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
        try:
            r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode != 0:                  # 超时 124 / 找不到进程 / ps 自己出错：一律「查不到」，不拿半截输出当启动时间
            return None
        return r.stdout.strip() or None

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def post_json(self, url, payload, timeout=10) -> dict:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")

    def env(self, key):
        return os.environ.get(key)
