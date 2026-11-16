#!/usr/bin/env python3
"""假 claude：照真 claude -p --output-format json 的样子，进程结束时往 stdout 写一个结果对象。

环境变量：
  FAKE_CLAUDE_SLEEP     先睡这么多秒（模拟长跑 worker）
  FAKE_CLAUDE_ARGV_LOG  把 argv 追加到这个文件（一行一个 JSON 数组）
  FAKE_CLAUDE_HOOK      退出前执行的脚本（python 文件），用来模拟 worker 写 progress / 事件
  FAKE_CLAUDE_NO_RESULT 设了就不写结果（模拟进程被杀、Mac 睡眠）
  OUT_JSON              设了就把它的内容原样写到 prompt 里 `OUT=<路径>` 指的文件（模拟分析 agent 写产出）
  FAKE_CLAUDE_SESSIONS  严格模式（opt-in）：会话登记文件（JSON：session id → 首发时的 cwd）。照真 claude 的两条规矩：
                        `--session-id X` 已登记过 ⇒「Session ID X is already in use」；
                        `--resume X` 没登记过、或登记的 cwd 不是当前目录 ⇒「No conversation found」（claude 按 cwd 找会话）。
                        出错时写 is_error 结果、退出 1，不跑 hook。
"""
import fcntl
import json
import os
import re
import runpy
import sys
import time

argv = sys.argv[1:]


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


def strict_sessions():
    """→ 错误文本 | None。读改写整份登记文件时持排他锁（并发起的两个假 claude 不互相覆盖）。"""
    path = os.environ.get("FAKE_CLAUDE_SESSIONS")
    if not path:
        return None
    cwd = os.path.realpath(os.getcwd())
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        reg = json.loads(f.read() or "{}")
        sid = opt("--session-id")
        if sid:
            if sid in reg:
                return f"Error: Session ID {sid} is already in use."
            reg[sid] = cwd
            f.seek(0); f.truncate(); f.write(json.dumps(reg))
            return None
        sid = opt("--resume")
        if sid and (sid not in reg or os.path.realpath(reg[sid]) != cwd):
            return f"No conversation found with session ID: {sid}"
    return None


if os.environ.get("FAKE_CLAUDE_ARGV_LOG"):
    with open(os.environ["FAKE_CLAUDE_ARGV_LOG"], "a") as f:
        f.write(json.dumps(argv, ensure_ascii=False) + "\n")
_strict_err = strict_sessions()
if _strict_err:
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": _strict_err,
                      "session_id": opt("--session-id") or opt("--resume"),
                      "router_ticket": os.environ.get("ROUTER_TICKET"), "cwd": os.getcwd()}))
    sys.exit(1)
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
if "OUT_JSON" in os.environ:
    m = re.search(r"OUT=(\S+)", opt("-p") or "")
    if m:
        os.makedirs(os.path.dirname(m.group(1)) or ".", exist_ok=True)
        with open(m.group(1), "w", encoding="utf-8") as f:
            f.write(os.environ["OUT_JSON"])
if os.environ.get("FAKE_CLAUDE_HOOK"):
    sys.argv = [os.environ["FAKE_CLAUDE_HOOK"], *argv]
    runpy.run_path(os.environ["FAKE_CLAUDE_HOOK"], run_name="__main__")
if not os.environ.get("FAKE_CLAUDE_NO_RESULT"):
    print("some streaming noise {not json")
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok",
                      "session_id": opt("--session-id") or opt("--resume"),
                      "router_ticket": os.environ.get("ROUTER_TICKET"),
                      "claudecode": os.environ.get("CLAUDECODE")}))
