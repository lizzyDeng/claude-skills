#!/usr/bin/env python3
"""
E2E Runner — 通用编排器 + 数据采集。

只负责：按场景执行操作、采集响应数据、输出结构化结果。
不做任何判断。判断由 LLM（Claude Code 会话中）完成。

这是一个模板/参考实现。每个项目应根据自身技术栈定制 Runner，
但必须遵循核心原则：Runner 只采集，不判断。

用法:
  # 跑场景，输出 JSON 结果
  python3 tests/e2e_runner.py -o /tmp/e2e_result.json

  # 指定场景文件
  python3 tests/e2e_runner.py --scenario tests/e2e_scenarios/xxx.json -o /tmp/e2e_result.json

  # 指定被测服务地址
  python3 tests/e2e_runner.py --base-url http://localhost:8080 -o /tmp/e2e_result.json

  # 附加日志采集
  python3 tests/e2e_runner.py --log-file /tmp/server.log -o /tmp/e2e_result.json

场景 JSON 格式:
{
  "name": "场景名",
  "description": "验证什么",
  "setup": { ... },              // 可选：前置命令/状态
  "turns": [
    {
      "action": "POST /api/endpoint",
      "headers": { "x-key": "value" },
      "body": { "field": "value" },
      "delay": 2
    }
  ],
  "teardown": { ... },           // 可选：清理
  "repeat": 2                    // 重复次数（LLM 场景建议 ≥2）
}

输出 JSON（给 LLM 判断用）:
{
  "scenarios": [{
    "name": "...",
    "description": "...",
    "rounds": [{
      "turns": [{
        "action": "POST /api/endpoint",
        "input": { ... },
        "status": 200,
        "response": { ... },
        "elapsed_ms": 500,
        "log_context": { ... }    // 从日志提取的上下文（可选）
      }]
    }]
  }]
}
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests")
    sys.exit(1)


def parse_action(action_str):
    """解析 'METHOD /path' 格式"""
    parts = action_str.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0].upper(), parts[1]
    return "GET", parts[0] if parts else "/"


def execute_turn(base_url, turn, default_headers=None, timeout=30):
    """执行单个 turn，返回结构化结果"""
    action = turn.get("action", "GET /")
    method, path = parse_action(action)
    url = f"{base_url}{path}" if path.startswith("/") else path

    headers = {**(default_headers or {}), **(turn.get("headers", {}))}
    body = turn.get("body")

    start = time.time()
    try:
        resp = requests.request(
            method, url,
            json=body if body else None,
            headers=headers,
            timeout=timeout,
            stream=turn.get("stream", False),
        )

        # 处理 SSE 流式响应
        if turn.get("stream"):
            text_parts = []
            extra_events = []
            for line in resp.iter_lines():
                decoded = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
                if not decoded or not decoded.startswith("data: "):
                    continue
                try:
                    event = json.loads(decoded[6:])
                    if event.get("type") == "delta":
                        text_parts.append(event.get("content", ""))
                    else:
                        extra_events.append(event)
                except json.JSONDecodeError:
                    pass
            response_data = {
                "text": "".join(text_parts).strip(),
                "events": extra_events if extra_events else None,
            }
        else:
            try:
                response_data = resp.json()
            except (json.JSONDecodeError, ValueError):
                response_data = resp.text

        return {
            "status": resp.status_code,
            "response": response_data,
            "elapsed_ms": int((time.time() - start) * 1000),
            "error": None,
        }
    except requests.exceptions.RequestException as e:
        return {
            "status": None,
            "response": None,
            "elapsed_ms": int((time.time() - start) * 1000),
            "error": str(e),
        }


def parse_log_context(log_file, patterns=None):
    """从日志文件尾部提取上下文信息（可选）"""
    if not log_file or not os.path.isfile(log_file):
        return {}
    if not patterns:
        return {}

    info = {}
    try:
        with open(log_file, "r") as f:
            lines = f.readlines()[-200:]  # 只看最后 200 行
    except (FileNotFoundError, IOError):
        return info

    ansi_re = re.compile(r'\x1b\[[0-9;]*m')
    for line in reversed(lines):
        line = ansi_re.sub('', line)
        for key, pattern in patterns.items():
            if key not in info:
                m = re.search(pattern, line)
                if m:
                    info[key] = m.group(1) if m.groups() else True
        if len(info) >= len(patterns):
            break
    return info


def run_scenario(scenario, base_url, log_file=None, log_patterns=None):
    """运行一个场景，返回结构化结果"""
    name = scenario["name"]
    repeat = scenario.get("repeat", 1)
    default_headers = scenario.get("headers", {})

    print(f"Running: {name} ({repeat} round(s))")

    # Setup
    setup_cmd = scenario.get("setup", {}).get("command")
    if setup_cmd:
        subprocess.run(setup_cmd, shell=True, timeout=30)

    rounds = []
    for rnd in range(repeat):
        turns = []
        for turn in scenario["turns"]:
            delay = turn.get("delay", 1)

            result = execute_turn(base_url, turn, default_headers)
            time.sleep(delay)

            # 可选：从日志采集上下文
            log_context = parse_log_context(log_file, log_patterns) if log_file else {}

            turn_result = {
                "action": turn.get("action", ""),
                "input": turn.get("body", turn.get("content", "")),
                **result,
            }
            if log_context:
                turn_result["log_context"] = log_context

            turns.append(turn_result)

            # Print progress
            resp_short = str(result.get("response", ""))[:60]
            status = result.get("status", "ERR")
            print(f"  [{rnd+1}/{repeat}] {turn.get('action', '')} → {status} {resp_short}")

        rounds.append({"turns": turns})

    # Teardown
    teardown_cmd = scenario.get("teardown", {}).get("command")
    if teardown_cmd:
        subprocess.run(teardown_cmd, shell=True, timeout=30)

    return {
        "name": name,
        "description": scenario.get("description", ""),
        "rounds": rounds,
    }


def load_scenarios(path):
    """加载场景：支持单文件或目录"""
    if os.path.isfile(path):
        with open(path) as f:
            data = json.load(f)
        return [data] if isinstance(data, dict) else data
    elif os.path.isdir(path):
        scenarios = []
        for fp in sorted(glob.glob(os.path.join(path, "*.json"))):
            with open(fp) as f:
                data = json.load(f)
            scenarios.extend(data if isinstance(data, list) else [data])
        return scenarios
    print(f"ERROR: {path} not found")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="E2E Runner — 通用编排器")
    parser.add_argument("--base-url", default="http://localhost:3000",
                       help="被测服务地址")
    parser.add_argument("--log-file", default=None,
                       help="服务日志文件路径（可选，用于采集 pipeline 上下文）")
    parser.add_argument("--scenario", default="tests/e2e_scenarios/",
                       help="场景 JSON 文件或目录")
    parser.add_argument("--health", default="/health",
                       help="健康检查端点（设为空字符串跳过）")
    parser.add_argument("-o", "--output", default="/tmp/e2e_result.json",
                       help="输出 JSON 路径（给 LLM 判断）")
    args = parser.parse_args()

    # 健康检查
    if args.health:
        try:
            requests.get(f"{args.base_url}{args.health}", timeout=5)
        except Exception:
            print(f"ERROR: Server not reachable at {args.base_url}{args.health}")
            sys.exit(1)

    scenarios = load_scenarios(args.scenario)
    print(f"Loaded {len(scenarios)} scenario(s)\n")

    results = [run_scenario(s, args.base_url, args.log_file) for s in scenarios]

    output = {"scenarios": results, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    total_turns = sum(
        len(t) for s in results for r in s["rounds"] for t in [r["turns"]]
    )
    print(f"\nDone. {total_turns} turns across {len(results)} scenarios.")
    print(f"Results written to {args.output}")
    print("→ Read the JSON and let LLM judge the results.")


if __name__ == "__main__":
    main()
