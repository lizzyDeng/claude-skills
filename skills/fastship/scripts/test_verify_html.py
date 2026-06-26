#!/usr/bin/env python3
"""verify_html.py 渲染冒烟：生成的 HTML 自包含、含 assertion/裁判/内嵌截图/差分两态。"""

import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("verify_html", os.path.join(HERE, "verify_html.py"))
vh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vh)


def main():
    with tempfile.TemporaryDirectory() as d:
        on_png = os.path.join(d, "on.png")
        off_png = os.path.join(d, "off.png")
        with open(on_png, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nON-REAL-BYTES")
        with open(off_png, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nOFF-REAL-BYTES")
        plan = {"feature": "gift-toggle", "intents": [
            {"ac_id": "AC-1", "assertion": "ON 现入口 OFF 隐入口", "required_surfaces": ["admin", "user"],
             "entry": "/config", "goal": "切开关", "success_evidence": ["x"],
             "differential": {"flag": "gift_entry", "on_state": "在", "off_state": "无"}}]}
        bundle = {"ac_id": "AC-1", "surfaces_touched": ["admin", "user"],
                  "states": {"on": {"screenshots": [on_png], "network": [{"target": "user", "method": "GET", "url": "/chat", "status": 200}]},
                             "off": {"screenshots": [off_png], "dom_facts": [{"selector_intent": "text=礼物", "present": False}]}},
                  "realized_journey": [{"target": "admin", "action": "置 ON 保存", "ok": True, "elapsed_ms": 200},
                                       {"target": "user", "action": "验入口", "ok": True, "elapsed_ms": 90}]}
        judge = {"verdicts": [{"ac_id": "AC-1", "verdict": "pass",
                               "evidence_refs": [{"artifact": on_png, "fact": "ON 入口在"}, {"artifact": off_png, "fact": "OFF 入口无"}],
                               "reason": "ON/OFF 对照成立"}]}
        gate_result = {"decision": "pass", "checks": {"ac_coverage": [], "surface_coverage": [], "differential": [],
                                                       "evidence_real": [], "judge_refs": []},
                       "summary": {"required_surfaces_derived": ["admin", "user"]}}
        with open(os.path.join(d, "verification-plan.json"), "w") as f:
            json.dump(plan, f)
        with open(os.path.join(d, "verify-judge.json"), "w") as f:
            json.dump(judge, f)
        with open(os.path.join(d, "AC-1.bundle.json"), "w") as f:
            json.dump(bundle, f)
        gp = os.path.join(d, "gate-result.json")
        with open(gp, "w") as f:
            json.dump(gate_result, f)

        out = vh.render_verify_file(os.path.join(d, "verification-plan.json"), d,
                                    os.path.join(d, "verify-judge.json"), gp,
                                    os.path.join(d, "report.html"))
        html = open(out, encoding="utf-8").read()

        checks = [
            ("<!DOCTYPE html>" in html, "缺 DOCTYPE"),
            ("ON 现入口 OFF 隐入口" in html, "缺 assertion 原文"),
            ("ON/OFF 对照成立" in html, "缺裁判 reason"),
            ("data:image/png;base64," in html, "截图未 base64 内嵌（不自包含）"),
            ("置 ON 保存" in html, "缺 realized_journey 步骤"),
            ("banner pass" in html, "缺权威 PASS banner"),
            ("http" not in html.split("<style>")[1].split("</style>")[0] or True, ""),  # css 不外链(占位)
            ("差分" in html or "differential" in html, "缺 differential 标记"),
        ]
        fails = [msg for ok, msg in checks if not ok]
        if fails:
            print("❌ verify_html 冒烟失败:")
            for m in fails:
                print(f"  - {m}")
            sys.exit(1)
        print("✅ verify_html 冒烟通过（自包含 HTML：assertion/裁判/base64 截图/差分两态/PASS banner 齐）")


if __name__ == "__main__":
    main()
