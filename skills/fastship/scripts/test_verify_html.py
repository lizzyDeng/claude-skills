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
    _smoke_large_image_embeds()


def _smoke_large_image_embeds():
    """大图（>4MB，旧硬上限）必须仍被内嵌——不再静默丢成「截图未内嵌」外链。

    分两类：① 真·大图（Pillow 生成的高分辨率噪声 PNG）走降采样路线；
    ② 非法/损坏的 >4MB 文件走原图直嵌兜底（≤16MB）。两者都必须出现在报告里。"""
    with tempfile.TemporaryDirectory() as d:
        big_real = os.path.join(d, "big-real.png")
        big_raw = os.path.join(d, "big-raw.png")

        real_is_large = False
        try:
            from PIL import Image  # noqa
            import random as _r
            _r.seed(7)
            w, h = 2600, 2000  # 远超 _DOWNSCALE_MAX_W=1600；噪声不可压 → 文件远超 512KB 触发降采样
            im = Image.new("RGB", (w, h))
            im.putdata([(_r.randint(0, 255), _r.randint(0, 255), _r.randint(0, 255)) for _ in range(w * h)])
            im.save(big_real, format="PNG")
            real_is_large = os.path.getsize(big_real) > 4 * 1024 * 1024
        except Exception:
            real_is_large = False

        # 非法 PNG 字节，但体积 >4MB：降采样打不开 → 必须走原图直嵌兜底
        with open(big_raw, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024))

        plan = {"feature": "big-shot", "journeys": [
            {"id": "main", "title": "大图内嵌", "steps": [
                {"no": 1, "surface": "user", "action": "看大图A", "proves_acs": ["AC-1"]},
                {"no": 2, "surface": "user", "action": "看大图B", "proves_acs": ["AC-1"]}]}]}
        journey = {"journey_id": "main", "title": "大图内嵌", "steps": [
            {"no": 1, "surface": "user", "state": "default", "action": "看大图A", "ok": True,
             "screenshot": big_real, "caption": "高分辨率真图", "proves_acs": ["AC-1"]},
            {"no": 2, "surface": "user", "state": "default", "action": "看大图B", "ok": True,
             "screenshot": big_raw, "caption": "5MB 兜底图", "proves_acs": ["AC-1"]}]}
        judge = {"verdicts": [{"ac_id": "AC-1", "verdict": "pass", "evidence_refs": [], "reason": "ok"}]}
        for name, obj in (("verification-plan.json", plan), ("verify-judge.json", judge),
                          ("main.journey.json", journey)):
            with open(os.path.join(d, name), "w") as f:
                json.dump(obj, f)

        out = vh.render_verify_file(os.path.join(d, "verification-plan.json"), d,
                                    os.path.join(d, "verify-judge.json"), None,
                                    os.path.join(d, "report.html"))
        html = open(out, encoding="utf-8").read()

        problems = []
        # 5MB 兜底图：旧逻辑会被 4MB 上限丢弃，新逻辑必须内嵌
        if "data:image/png;base64," not in html:
            problems.append("5MB 兜底图未内嵌（疑似仍受 4MB 旧上限）")
        if "截图未内嵌" in html:
            problems.append(f"出现退化文案「截图未内嵌」：{html[html.find('截图未内嵌'):][:80]}")
        # Pillow 在且生成了真·大图时，降采样路线应产出 JPEG data URI
        if real_is_large and "data:image/jpeg;base64," not in html:
            problems.append("真·大图未走降采样（缺 JPEG data URI）")

        if problems:
            print("❌ verify_html 大图内嵌冒烟失败:")
            for m in problems:
                print(f"  - {m}")
            sys.exit(1)
        extra = "（Pillow 在：真图降采样为 JPEG + 5MB 兜底直嵌）" if real_is_large \
            else "（Pillow 不可用/未触发：5MB 兜底直嵌已验）"
        print(f"✅ verify_html 大图内嵌冒烟通过：>4MB 截图不再被丢弃 {extra}")


if __name__ == "__main__":
    main()
