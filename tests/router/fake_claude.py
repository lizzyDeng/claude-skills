#!/usr/bin/env python3
"""假 claude：照真 claude -p --output-format json 的样子，进程结束时往 stdout 写一个结果对象。

环境变量：
  FAKE_CLAUDE_SLEEP     先睡这么多秒（模拟长跑 worker）
  FAKE_CLAUDE_ARGV_LOG  把 argv 追加到这个文件（一行一个 JSON 数组）
  FAKE_CLAUDE_HOOK      退出前执行的脚本（python 文件），用来模拟 worker 写 progress / 事件
  FAKE_CLAUDE_NO_RESULT 设了就不写结果（模拟进程被杀、Mac 睡眠）
"""
import json
import os
import runpy
import sys
import time

argv = sys.argv[1:]


def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None


if os.environ.get("FAKE_CLAUDE_ARGV_LOG"):
    with open(os.environ["FAKE_CLAUDE_ARGV_LOG"], "a") as f:
        f.write(json.dumps(argv, ensure_ascii=False) + "\n")
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
if os.environ.get("FAKE_CLAUDE_HOOK"):
    sys.argv = [os.environ["FAKE_CLAUDE_HOOK"], *argv]
    runpy.run_path(os.environ["FAKE_CLAUDE_HOOK"], run_name="__main__")
if not os.environ.get("FAKE_CLAUDE_NO_RESULT"):
    print("some streaming noise {not json")
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok",
                      "session_id": opt("--session-id") or opt("--resume"),
                      "router_ticket": os.environ.get("ROUTER_TICKET"),
                      "claudecode": os.environ.get("CLAUDECODE")}))
