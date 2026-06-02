#!/usr/bin/env python3
"""
Smell-test gate regression — e2e_gate.py must FAIL on degenerate results.

动机（推文）：人看到「reward=0.0 / 10 轮全空回复」会先怀疑自己的 setup；
agent 会照单全收说「被测对象就是这么烂」。旧 gate 对空回复只打 warning 仍 PASS，
就是这个「耸耸肩」失败。本测试钉死：极端退化结果必须 exit 1。

校准：provider-ejection runner 产出的 turn 里 elapsed_ms 恒为 0、response 为非空 dict、
健康跑 0 空回复 —— 所以 gate 只能基于「空回复率 / 错误率」判退化，禁止用延迟做信号，
否则会误伤这个合法 runner。下面的 healthy_* 用例就是它的真实形状。

跑法: python3 tests/test_e2e_gate_smell.py
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "e2e_gate.py")


def _ejection_turn(idx, *, empty=False, error=False):
    """One turn shaped exactly like e2e_provider_ejection_runner.fastship_scenarios."""
    if empty:
        response = None
    else:
        response = {"done": True, "event_types": ["delta", "done"], "checks": None}
    return {
        "action": "POST /api/chat/send",
        "input": {"content": f"m{idx}", "phase": "before_cooldown"},
        "status": "error" if error else "done",
        "response": response,
        "elapsed_ms": 0,  # runner never measures latency — must NOT be a smell signal
        "error": "boom" if error else None,
    }


def _result(turns):
    return {
        "scenarios": [
            {
                "name": "provider_outlier_ejection_chat_send",
                "description": "calibration fixture",
                "rounds": [{"turns": turns}],
            }
        ]
    }


def _run_gate(result_obj):
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(result_obj, f)
        proc = subprocess.run(
            [sys.executable, GATE, "--result", path, "--min-turns", "10"],
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode, proc.stdout + proc.stderr
    finally:
        os.unlink(path)


CASES = [
    # (name, turns, expected_exit)
    ("healthy_11_all_done",
     [_ejection_turn(i) for i in range(11)], 0),
    ("all_empty_11",
     [_ejection_turn(i, empty=True) for i in range(11)], 1),
    ("half_empty_6_of_11",
     [_ejection_turn(i, empty=(i < 6)) for i in range(11)], 1),
    ("one_empty_of_11_is_warning_not_fail",
     [_ejection_turn(i, empty=(i == 0)) for i in range(11)], 0),
    ("all_errors_11",
     [_ejection_turn(i, error=True) for i in range(11)], 1),
]


def main():
    failures = []
    for name, turns, expected in CASES:
        code, out = _run_gate(_result(turns))
        ok = code == expected
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: exit={code} (expected {expected})")
        if not ok:
            failures.append((name, expected, code, out[-400:]))
    print()
    if failures:
        print(f"❌ {len(failures)}/{len(CASES)} smell-test cases failed:")
        for name, expected, code, tail in failures:
            print(f"  - {name}: expected exit {expected}, got {code}")
            print(f"    …{tail}")
        sys.exit(1)
    print(f"✅ all {len(CASES)} smell-test cases passed")


if __name__ == "__main__":
    main()
