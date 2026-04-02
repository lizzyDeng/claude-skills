#!/usr/bin/env python3
"""
E2E Gate — 合并前强制检查（通用版）。

由 hook 或手动调用。检查 e2e_result.json 是否存在、数据是否充分、
并把原始数据输出给用户看（防止 LLM 偷懒编造通过结果）。

设计原则：LLM 输出的质量报告可能不诚实，所以 Gate 必须：
1. 独立验证数据充分性（不依赖 LLM 的报告）
2. 把原始数据直接展示给用户（用户自己判断 LLM 报告是否诚实）

用法:
  python3 tests/e2e_gate.py [--result /tmp/e2e_result.json] [--min-turns 10]

退出码:
  0 = gate 通过（数据充分，用户可以审查）
  1 = gate 失败（数据不足或文件不存在）
"""

import argparse
import json
import os
import sys
import time


def main():
    parser = argparse.ArgumentParser(description="E2E Gate — 合并前检查")
    parser.add_argument("--result", default="/tmp/e2e_result.json",
                       help="e2e_runner 输出的结果文件")
    parser.add_argument("--min-turns", type=int, default=10,
                       help="最少调用轮数")
    parser.add_argument("--max-age-minutes", type=int, default=30,
                       help="结果文件最大年龄（分钟）")
    args = parser.parse_args()

    print("=" * 60)
    print("E2E GATE — 合并前检查")
    print("=" * 60)

    # Check 1: 文件存在
    if not os.path.isfile(args.result):
        print(f"\n❌ BLOCKED: {args.result} 不存在")
        print("  → 先跑 e2e_runner.py 生成结果")
        sys.exit(1)

    # Check 2: 文件不能太旧
    age_seconds = time.time() - os.path.getmtime(args.result)
    age_minutes = age_seconds / 60
    if age_minutes > args.max_age_minutes:
        print(f"\n❌ BLOCKED: 结果文件已过期（{int(age_minutes)} 分钟前生成，上限 {args.max_age_minutes} 分钟）")
        print("  → 重新跑 e2e_runner.py")
        sys.exit(1)

    # Check 3: 解析数据
    try:
        with open(args.result, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"\n❌ BLOCKED: 结果文件解析失败: {e}")
        sys.exit(1)

    scenarios = data.get("scenarios", [])
    if not scenarios:
        print("\n❌ BLOCKED: 结果文件中没有场景数据")
        sys.exit(1)

    # Check 4: 统计覆盖度
    total_turns = 0
    total_empty = 0
    total_errors = 0
    for scenario in scenarios:
        for rnd in scenario.get("rounds", []):
            for turn in rnd.get("turns", []):
                total_turns += 1
                response = turn.get("response")
                if not response:
                    total_empty += 1
                if turn.get("error"):
                    total_errors += 1

    print(f"\n总调用轮数: {total_turns} (最低要求: {args.min_turns})")
    print(f"场景数: {len(scenarios)}")
    print(f"空回复数: {total_empty}")
    print(f"错误数: {total_errors}")

    if total_turns < args.min_turns:
        print(f"\n❌ BLOCKED: 调用轮数不足 ({total_turns} < {args.min_turns})")
        print("  → 增加场景或 repeat 数")
        sys.exit(1)

    if total_empty > 0:
        print(f"\n⚠️ 警告: 有 {total_empty} 轮空回复")

    if total_errors > 0:
        print(f"\n⚠️ 警告: 有 {total_errors} 轮出错")

    # Check 5: 输出原始数据给用户审查
    print("\n" + "=" * 60)
    print("原始数据（用户审查用，不要只看 LLM 的总结）")
    print("=" * 60)

    for scenario in scenarios:
        print(f"\n### {scenario['name']}")
        if scenario.get("description"):
            print(f"    {scenario['description']}")

        for ri, rnd in enumerate(scenario.get("rounds", [])):
            if len(scenario.get("rounds", [])) > 1:
                print(f"\n  --- Round {ri + 1} ---")

            for ti, turn in enumerate(rnd.get("turns", [])):
                action = turn.get("action", "")
                input_data = turn.get("input", "")
                response = turn.get("response", "")
                status = turn.get("status", "")
                error = turn.get("error", "")
                elapsed = turn.get("elapsed_ms", 0)
                log_ctx = turn.get("log_context", {})

                print(f"\n  Turn {ti}:")
                print(f"    操作: {action}")
                if input_data:
                    input_str = json.dumps(input_data, ensure_ascii=False) if isinstance(input_data, dict) else str(input_data)
                    print(f"    输入: {input_str[:200]}")
                print(f"    状态: {status} ({elapsed}ms)")

                if error:
                    print(f"    ❌ 错误: {error}")
                else:
                    resp_str = json.dumps(response, ensure_ascii=False) if isinstance(response, (dict, list)) else str(response)
                    # 长响应截断展示
                    if len(resp_str) > 300:
                        print(f"    回复: {resp_str[:300]}...")
                    else:
                        print(f"    回复: {resp_str}")

                if log_ctx:
                    print(f"    日志上下文:")
                    for k, v in log_ctx.items():
                        v_str = str(v)[:100]
                        print(f"      {k}: {v_str}")

    print("\n" + "=" * 60)
    print("✅ GATE PASSED — 数据充分，请用户审查上述原始数据")
    print("   LLM 的质量报告是否诚实？对照上面的原始数据判断")
    print("=" * 60)


if __name__ == "__main__":
    main()
