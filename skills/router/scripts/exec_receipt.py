#!/usr/bin/env python3
"""启动 wrapper：ctx.Ctx.spawn_detached 用它起每一个后台进程。

  python3 -I exec_receipt.py -- <真正的命令…>

1. env 里有 ROUTER_RECEIPT：先把启动回执 {session_id, pid, pid_start, at} 原子写到那个路径。
   pid 就是本进程（exec 不换 pid，也不换进程启动时间），pid_start 与 ctx.Ctx.proc_start 同一条 ps 命令。
   router 事后按回执 + launcher.worker_alive（pid + 启动时间）认领 worker，不靠 argv、不扫全系统 ps（H4）。
2. exec 真正的命令。ROUTER_EXEC_FD 是父进程给的一条管道：exec 成功时它随 close-on-exec 关闭，
   父进程读到 EOF 即「已起」；exec 失败（或回执写不进去）就把错误写进管道再退出，父进程照 Popen 的语义抛 OSError。
回执写不进去时不 exec：没有回执的 worker，崩溃后的对账无法确认它起没起。
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _write_receipt(path: Path, sid: str) -> None:
    pid = os.getpid()
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=10)
        start = (r.stdout.strip() or None) if r.returncode == 0 else None   # rc≠0：不拿半截输出当启动时间
    except (OSError, subprocess.SubprocessError):
        start = None                           # 查不到启动时间：回执照写，router 对账时按「判活失败」处理
    body = json.dumps({"session_id": sid, "pid": pid, "pid_start": start,
                       "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv) -> int:
    if argv[:1] == ["--"]:
        argv = argv[1:]
    fd = int(os.environ.pop("ROUTER_EXEC_FD", "-1") or -1)
    receipt = os.environ.pop("ROUTER_RECEIPT", "")
    sid = os.environ.pop("ROUTER_RECEIPT_SID", "")
    try:
        if not argv:
            raise OSError(22, "exec_receipt：没有要执行的命令")
        if receipt:
            _write_receipt(Path(receipt), sid)
        if fd >= 0:
            os.set_inheritable(fd, False)      # exec 成功 ⇒ 管道随之关闭 ⇒ 父进程读到 EOF
        os.execvp(argv[0], argv)
    except OSError as e:
        if receipt:
            try:
                os.unlink(receipt)             # 没 exec 成：不留会被当成「起过」的回执
            except OSError:
                pass
        msg = {"errno": e.errno or 0, "strerror": e.strerror or str(e),
               "filename": e.filename or (argv[0] if argv else None)}
        print(f"exec_receipt：{msg['strerror']}（{argv[0] if argv else ''}）", file=sys.stderr)
        if fd >= 0:
            try:
                os.write(fd, json.dumps(msg, ensure_ascii=False).encode())
            except OSError:
                pass
        return 127
    return 0                                   # 不会走到：execvp 成功不返回


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
