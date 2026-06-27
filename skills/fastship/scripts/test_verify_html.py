#!/usr/bin/env python3
"""verify_html.py 渲染冒烟：生成的 HTML 自包含、含旅程时间线/每步截图/裁判/差分两态。"""

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
        on_png = os.path.join(d, "s2-on.png")
        off_png = os.path.join(d, "s4-off.png")
        a_on = os.path.join(d, "s1-on.png")
        a_off = os.path.join(d, "s3-off.png")
        for p, b in ((on_png, b"\x89PNG\r\n\x1a\nON-REAL-BYTES"), (off_png, b"\x89PNG\r\n\x1a\nOFF-REAL-BYTES"),
                     (a_on, b"\x89PNG\r\n\x1a\nADMIN-ON"), (a_off, b"\x89PNG\r\n\x1a\nADMIN-OFF")):
            with open(p, "wb") as f:
                f.write(b)
        plan = {"feature": "gift-toggle", "journeys": [
            {"id": "toggle", "title": "礼物入口开关对照",
             "differential": {"flag": "gift_entry", "on_state": "在", "off_state": "无"},
             "steps": [
                 {"no": 1, "surface": "admin", "action": "toggle", "proves_acs": []},
                 {"no": 2, "surface": "user", "action": "observe", "proves_acs": ["AC-1"]},
                 {"no": 3, "surface": "admin", "action": "toggle", "proves_acs": []},
                 {"no": 4, "surface": "user", "action": "observe", "proves_acs": ["AC-1"]}]}]}
        journey = {"journey_id": "toggle", "title": "礼物入口开关对照", "steps": [
            {"no": 1, "surface": "admin", "state": "on", "action": "置 ON 保存", "ok": True,
             "screenshot": a_on, "caption": "后台开开关", "proves_acs": []},
            {"no": 2, "surface": "user", "state": "on", "action": "验入口可见", "ok": True,
             "screenshot": on_png, "caption": "用户端礼物入口出现", "proves_acs": ["AC-1"],
             "network": [{"target": "user", "method": "GET", "url": "/chat", "status": 200}]},
            {"no": 3, "surface": "admin", "state": "off", "action": "置 OFF 保存", "ok": True,
             "screenshot": a_off, "caption": "后台关开关", "proves_acs": []},
            {"no": 4, "surface": "user", "state": "off", "action": "验入口消失", "ok": True,
             "screenshot": off_png, "caption": "用户端礼物入口消失", "proves_acs": ["AC-1"],
             "dom_facts": [{"selector_intent": "text=礼物", "present": False}]}]}
        judge = {"verdicts": [{"ac_id": "AC-1", "verdict": "pass",
                               "evidence_refs": [{"artifact": on_png, "fact": "ON 入口在"},
                                                 {"artifact": off_png, "fact": "OFF 入口无"}],
                               "reason": "ON/OFF 对照成立"}]}
        gate_result = {"decision": "pass", "checks": {"ac_coverage": [], "surface_coverage": [], "differential": [],
                                                       "evidence_real": [], "judge_refs": []},
                       "summary": {"required_surfaces_derived": ["admin", "user"],
                                   "surfaces_walked": ["admin", "user"]}}
        with open(os.path.join(d, "verification-plan.json"), "w") as f:
            json.dump(plan, f)
        with open(os.path.join(d, "verify-judge.json"), "w") as f:
            json.dump(judge, f)
        with open(os.path.join(d, "toggle.journey.json"), "w") as f:
            json.dump(journey, f)
        gp = os.path.join(d, "gate-result.json")
        with open(gp, "w") as f:
            json.dump(gate_result, f)

        out = vh.render_verify_file(os.path.join(d, "verification-plan.json"), d,
                                    os.path.join(d, "verify-judge.json"), gp,
                                    os.path.join(d, "report.html"))
        html = open(out, encoding="utf-8").read()

        checks = [
            ("<!DOCTYPE html>" in html, "缺 DOCTYPE"),
            ("用户旅程" in html, "缺旅程时间线标题"),
            ("ON/OFF 对照成立" in html, "缺裁判 reason"),
            ("data:image/png;base64," in html, "截图未 base64 内嵌（不自包含）"),
            (html.count("data:image/png;base64,") >= 4, "应内嵌每一步的截图（≥4 张）"),
            ("用户端礼物入口出现" in html, "缺步骤 caption（点击路径人读）"),
            ("置 ON 保存" in html, "缺旅程步骤 action"),
            ("banner pass" in html, "缺权威 PASS banner"),
            ('stag on' in html and 'stag off' in html, "缺差分 ON/OFF 状态标"),
            ("AC-1" in html, "缺 AC 裁判小结"),
        ]
        fails = [msg for ok, msg in checks if not ok]
        if fails:
            print("❌ verify_html 冒烟失败:")
            for m in fails:
                print(f"  - {m}")
            sys.exit(1)
        print("✅ verify_html 冒烟通过（自包含 HTML：旅程时间线/每步 base64 截图/caption/差分两态/PASS banner 齐）")


if __name__ == "__main__":
    main()
