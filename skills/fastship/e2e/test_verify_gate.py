#!/usr/bin/env python3
"""
verify_gate.py 回归测试 —— 钉死 spec §13 的八条可证伪验收（AC 驱动【用户旅程】版）。

跑法: python3 test_verify_gate.py
纯函数直测 + 子进程跑 CLI 验退出码（0=PASS / 1=FAIL / 3=SURFACE）。
真实 temp 截图（真字节、sha256 真算），保证 ④ 证据真实 检查被真正执行。

验证单元 = 一条正常用户路径(journey)。每步真截图（API 步用 network/响应替代），
AC 是沿途检查点。
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "verify_gate.py")

# import 纯函数
_spec = importlib.util.spec_from_file_location("verify_gate", GATE)
vg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vg)


# ── fixture builders ──────────────────────────────────────────────────────────

def _write_png(path, content):
    with open(path, "wb") as f:
        f.write(content)
    return content


def _step(d, no, surface, *, proves_acs, action="click", state="default", ok=True,
          caption="", network=None, dom=None, api=None, shot=True, png=None, tag=""):
    """造一个真实旅程步骤，写真截图，返回 (step_dict, {path: sha256})。"""
    step = {"no": no, "surface": surface, "action": action, "state": state, "ok": ok,
            "caption": caption or action, "proves_acs": list(proves_acs)}
    manifest = {}
    if shot:
        png = png if png is not None else f"PNG-{no}-{state}-{surface}-{tag}".encode()
        sp = os.path.join(d, f"step{no}-{state}-{surface}{tag}.png")
        _write_png(sp, png)
        step["screenshot"] = sp
        manifest[sp] = hashlib.sha256(png).hexdigest()
    if network:
        step["network"] = network
    if dom:
        step["dom_facts"] = dom
    if api:
        step["api_responses"] = api
    return step, manifest


def _journey(jid, title, steps):
    return {"journey_id": jid, "title": title, "steps": steps}


def _plan_step(no, surface, proves_acs, action="click", intent="", expect=""):
    return {"no": no, "surface": surface, "action": action, "intent": intent or action,
            "expect": expect or "ok", "proves_acs": list(proves_acs)}


def _plan_journey(jid, title, steps, differential=None):
    j = {"id": jid, "title": title, "steps": steps}
    if differential:
        j["differential"] = differential
    return j


def _scenario(d, *, plan_journeys, real_journeys, verdicts, manifest, surfaces=None, changed=None):
    """落盘一套完整 scenario，返回跑 CLI 所需路径。"""
    plan = {"feature": "t", "journeys": plan_journeys}
    judge = {"verdicts": verdicts}
    with open(os.path.join(d, "verification-plan.json"), "w") as f:
        json.dump(plan, f)
    with open(os.path.join(d, "verify-judge.json"), "w") as f:
        json.dump(judge, f)
    for rj in real_journeys:
        with open(os.path.join(d, f"{rj['journey_id']}.journey.json"), "w") as f:
            json.dump(rj, f)
    with open(os.path.join(d, "evidence-manifest.json"), "w") as f:
        json.dump({"artifacts": manifest}, f)
    sc = surfaces if surfaces is not None else {
        "user": {"base_url": "u", "app_paths": ["apps/web-app"]},
        "admin": {"base_url": "a", "app_paths": ["apps/admin-web"]},
        "api": {"base_url": "p", "app_paths": ["services/api-server"]},
    }
    scp = os.path.join(d, "surfaces.json")
    with open(scp, "w") as f:
        json.dump(sc, f)
    return {
        "plan": os.path.join(d, "verification-plan.json"),
        "judge": os.path.join(d, "verify-judge.json"),
        "evidence_dir": d,
        "surfaces": scp,
        "changed": changed if changed is not None else [],
    }


def _run_cli(paths):
    cmd = [sys.executable, GATE, "--plan", paths["plan"], "--evidence-dir", paths["evidence_dir"],
           "--judge", paths["judge"], "--surfaces-config", paths["surfaces"],
           "--changed-files", ",".join(paths["changed"])]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout + p.stderr


def _eval(paths):
    """直接走纯函数 evaluate（拿 decision + checks）。"""
    plan = json.load(open(paths["plan"]))
    judge = json.load(open(paths["judge"]))
    journeys = vg._load_journeys(paths["evidence_dir"])
    manifest = (json.load(open(os.path.join(paths["evidence_dir"], "evidence-manifest.json"))) or {}).get("artifacts") or {}
    surfaces = json.load(open(paths["surfaces"]))
    return vg.evaluate(plan, journeys, judge, surfaces, paths["changed"], manifest, paths["evidence_dir"])


# ── 八条验收 ──────────────────────────────────────────────────────────────────

def case_pure_ui_passes(d):
    """§13.1 纯 UI feature 走完整路径，每步截图，结构 gate PASS（不数 turn）。"""
    s1, m1 = _step(d, 1, "user", proves_acs=[], action="open", caption="打开聊天页")
    s2, m2 = _step(d, 2, "user", proves_acs=["AC-1"], action="click", caption="点礼物送出，看到感谢气泡",
                   network=[{"target": "user", "method": "POST", "url": "/api/gift", "status": 201}])
    rj = _journey("main", "送礼并收到感谢", [s1, s2])
    p = _scenario(d,
                  plan_journeys=[_plan_journey("main", "送礼并收到感谢", [
                      _plan_step(1, "user", [], "open"),
                      _plan_step(2, "user", ["AC-1"], "click")])],
                  real_journeys=[rj],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": s2["screenshot"], "fact": "感谢气泡"}],
                             "reason": "截图+network 双证"}],
                  manifest={**m1, **m2}, changed=["apps/web-app/src/Gift.tsx"])
    code, out = _run_cli(p)
    assert code == 0, f"纯 UI 应 PASS(exit 0)，得 {code}\n{out[-600:]}"
    assert _eval(p)["min_turns_used"] is False, "§13.8 min_turns 不得参与判定"


def case_pure_api_passes(d):
    """§13.2 后端 API feature 走 http 路径，API 步无截图（用响应替代），PASS。"""
    s1, _ = _step(d, 1, "api", proves_acs=["AC-1"], action="POST /api/credits", shot=False,
                  network=[{"target": "api", "method": "POST", "url": "/api/credits", "status": 200}],
                  api=[{"ok": True}])
    rj = _journey("main", "发放积分", [s1])
    p = _scenario(d,
                  plan_journeys=[_plan_journey("main", "发放积分", [_plan_step(1, "api", ["AC-1"], "POST")])],
                  real_journeys=[rj],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": "network", "fact": "200"}], "reason": "响应 200"}],
                  manifest={}, changed=["services/api-server/src/credits.rs"])
    code, out = _run_cli(p)
    assert code == 0, f"纯 API 应 PASS，得 {code}\n{out[-600:]}"


def case_cross_end_toggle_passes(d):
    """§13.3 cross-端 toggle 旅程：admin+user 两表面、ON/OFF 两态各截图，PASS。"""
    s1, m1 = _step(d, 1, "admin", proves_acs=[], action="置 ON 保存", state="on", caption="后台开开关")
    s2, m2 = _step(d, 2, "user", proves_acs=["AC-1"], state="on", action="验入口可见", caption="用户端入口出现")
    s3, m3 = _step(d, 3, "admin", proves_acs=[], action="置 OFF 保存", state="off", caption="后台关开关")
    s4, m4 = _step(d, 4, "user", proves_acs=["AC-1"], state="off", action="验入口消失", caption="用户端入口消失")
    rj = _journey("toggle", "礼物入口开关对照", [s1, s2, s3, s4])
    p = _scenario(d,
                  plan_journeys=[_plan_journey("toggle", "礼物入口开关对照", [
                      _plan_step(1, "admin", [], "toggle"), _plan_step(2, "user", ["AC-1"], "observe"),
                      _plan_step(3, "admin", [], "toggle"), _plan_step(4, "user", ["AC-1"], "observe")],
                      differential={"flag": "gift_entry", "on_state": "入口可见", "off_state": "入口消失"})],
                  real_journeys=[rj],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": s2["screenshot"], "fact": "ON 入口在"},
                                               {"artifact": s4["screenshot"], "fact": "OFF 入口没"}],
                             "reason": "ON/OFF 对照成立"}],
                  manifest={**m1, **m2, **m3, **m4},
                  changed=["apps/admin-web/src/Config.tsx", "apps/web-app/src/Chat.tsx"])
    code, out = _run_cli(p)
    assert code == 0, f"cross-端 toggle 应 PASS，得 {code}\n{out[-600:]}"


def case_missing_p0_ac_fails(d):
    """§13.4 计划声明两条 AC，旅程只证到一条 → AC 覆盖 FAIL。"""
    s1, m1 = _step(d, 1, "user", proves_acs=["AC-1"], action="x")
    rj = _journey("main", "j", [s1])
    p = _scenario(d,
                  plan_journeys=[_plan_journey("main", "j", [
                      _plan_step(1, "user", ["AC-1"]), _plan_step(2, "user", ["AC-2"])])],
                  real_journeys=[rj],  # 只证 AC-1
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": s1["screenshot"], "fact": "f"}], "reason": "r"}],
                  manifest=m1, changed=["apps/web-app/x.tsx"])
    code, _ = _run_cli(p)
    assert code == 1, f"漏 P0 AC 应 FAIL，得 {code}"
    assert _eval(p)["checks"]["ac_coverage"], "应报 ac_coverage 失败"


def case_cross_end_skips_admin_fails(d):
    """§13.5 diff 改了 admin+user，旅程只走用户端没碰 admin → surface 覆盖 FAIL。"""
    s1, m1 = _step(d, 1, "user", proves_acs=["AC-1"], action="只点用户端")
    rj = _journey("main", "j", [s1])
    p = _scenario(d,
                  plan_journeys=[_plan_journey("main", "j", [_plan_step(1, "user", ["AC-1"])])],
                  real_journeys=[rj],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": s1["screenshot"], "fact": "f"}], "reason": "r"}],
                  manifest=m1, changed=["apps/admin-web/c.tsx", "apps/web-app/u.tsx"])
    code, _ = _run_cli(p)
    assert code == 1, f"没走 admin 应 FAIL，得 {code}"
    assert _eval(p)["checks"]["surface_coverage"], "应报 surface_coverage 失败"


def case_feature_surface_undeclared_fails(d):
    """§13.5 feature 级：diff 改了 admin 但旅程从没走 admin → surface 覆盖 FAIL（即便 AC 都过）。"""
    s1, m1 = _step(d, 1, "user", proves_acs=["AC-1"], action="x")
    rj = _journey("main", "j", [s1])
    p = _scenario(d,
                  plan_journeys=[_plan_journey("main", "j", [_plan_step(1, "user", ["AC-1"])])],
                  real_journeys=[rj],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": s1["screenshot"], "fact": "f"}], "reason": "r"}],
                  manifest=m1, changed=["apps/admin-web/c.tsx", "apps/web-app/u.tsx"])  # 改了 admin 却没走
    assert _eval(p)["checks"]["surface_coverage"], "改了 admin 没走应报 surface_coverage 失败"


def case_toggle_only_on_fails(d):
    """§13.6 toggle 旅程只走 ON 没走 OFF → differential FAIL。"""
    s1, m1 = _step(d, 1, "admin", proves_acs=[], state="on", action="置 ON")
    s2, m2 = _step(d, 2, "user", proves_acs=["AC-1"], state="on", action="验 ON")
    rj = _journey("toggle", "只走 ON", [s1, s2])  # 缺 off
    p = _scenario(d,
                  plan_journeys=[_plan_journey("toggle", "开关对照", [
                      _plan_step(1, "admin", [], "toggle"), _plan_step(2, "user", ["AC-1"], "observe")],
                      differential={"flag": "f", "on_state": "on", "off_state": "off"})],
                  real_journeys=[rj],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": s2["screenshot"], "fact": "f"}], "reason": "r"}],
                  manifest={**m1, **m2}, changed=["apps/admin-web/c.tsx", "apps/web-app/u.tsx"])
    code, _ = _run_cli(p)
    assert code == 1, f"只走 ON 应 FAIL，得 {code}"
    assert _eval(p)["checks"]["differential"], "应报 differential 失败"


def case_forged_judge_ref_fails(d):
    """§13.7 裁判判 pass 但 evidence_ref 指向不存在文件 → 反橡皮图章 FAIL。"""
    s1, m1 = _step(d, 1, "user", proves_acs=["AC-1"], action="x")
    rj = _journey("main", "j", [s1])
    p = _scenario(d,
                  plan_journeys=[_plan_journey("main", "j", [_plan_step(1, "user", ["AC-1"])])],
                  real_journeys=[rj],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": os.path.join(d, "ghost.png"), "fact": "我看到了"}],
                             "reason": "我说通过就通过"}],  # ghost.png 不存在且不属于该 AC 步骤
                  manifest=m1, changed=["apps/web-app/x.tsx"])
    code, _ = _run_cli(p)
    assert code == 1, f"裁判伪造引用应 FAIL，得 {code}"
    assert _eval(p)["checks"]["judge_refs"], "应报 judge_refs 失败"


def case_judge_ref_other_ac_step_fails(d):
    """§13.7 裁判引用了【别的 AC 的步骤截图】（存在但不属于本 AC）→ 反橡皮图章 FAIL。"""
    s1, m1 = _step(d, 1, "user", proves_acs=["AC-1"], action="证 AC-1")
    s2, m2 = _step(d, 2, "user", proves_acs=["AC-2"], action="证 AC-2")
    rj = _journey("main", "j", [s1, s2])
    p = _scenario(d,
                  plan_journeys=[_plan_journey("main", "j", [
                      _plan_step(1, "user", ["AC-1"]), _plan_step(2, "user", ["AC-2"])])],
                  real_journeys=[rj],
                  verdicts=[
                      {"ac_id": "AC-1", "verdict": "pass",
                       "evidence_refs": [{"artifact": s2["screenshot"], "fact": "借 AC-2 的图"}], "reason": "r"},
                      {"ac_id": "AC-2", "verdict": "pass",
                       "evidence_refs": [{"artifact": s2["screenshot"], "fact": "f"}], "reason": "r"}],
                  manifest={**m1, **m2}, changed=["apps/web-app/x.tsx"])
    code, _ = _run_cli(p)
    assert code == 1, f"借别的 AC 的图应 FAIL，得 {code}"
    assert _eval(p)["checks"]["judge_refs"], "应报 judge_refs 失败"


def case_tampered_artifact_fails(d):
    """④ 截图采集后被改（sha256 不符 manifest）→ FAIL。"""
    s1, m1 = _step(d, 1, "user", proves_acs=["AC-1"], action="x")
    with open(s1["screenshot"], "wb") as f:
        f.write(b"TAMPERED-DIFFERENT-BYTES")  # 篡改文件，manifest 仍存旧 hash
    rj = _journey("main", "j", [s1])
    p = _scenario(d,
                  plan_journeys=[_plan_journey("main", "j", [_plan_step(1, "user", ["AC-1"])])],
                  real_journeys=[rj],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": s1["screenshot"], "fact": "f"}], "reason": "r"}],
                  manifest=m1, changed=["apps/web-app/x.tsx"])
    assert _eval(p)["checks"]["evidence_real"], "篡改截图应报 evidence_real 失败"


def case_step_missing_screenshot_fails(d):
    """🔴每步截图：UI 步缺截图 → schema BLOCKED（exit 1）。"""
    s1, m1 = _step(d, 1, "user", proves_acs=["AC-1"], action="x", shot=False)  # UI 步却无截图
    rj = _journey("main", "j", [s1])
    p = _scenario(d,
                  plan_journeys=[_plan_journey("main", "j", [_plan_step(1, "user", ["AC-1"])])],
                  real_journeys=[rj],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": "dom", "fact": "f"}], "reason": "r"}],
                  manifest=m1, changed=["apps/web-app/x.tsx"])
    code, out = _run_cli(p)
    assert code == 1, f"UI 步缺截图应 BLOCKED(exit 1)，得 {code}"
    assert "截图" in out, "应明确报缺截图"


def case_uncertain_surfaces(d):
    """⑥ 任一 verdict=uncertain → SURFACE（exit 3，阻断等人）。"""
    s1, m1 = _step(d, 1, "user", proves_acs=["AC-1"], action="x")
    rj = _journey("main", "j", [s1])
    p = _scenario(d,
                  plan_journeys=[_plan_journey("main", "j", [_plan_step(1, "user", ["AC-1"])])],
                  real_journeys=[rj],
                  verdicts=[{"ac_id": "AC-1", "verdict": "uncertain",
                             "evidence_refs": [{"artifact": s1["screenshot"], "fact": "看不太清"}],
                             "reason": "截图模糊"}],
                  manifest=m1, changed=["apps/web-app/x.tsx"])
    code, _ = _run_cli(p)
    assert code == 3, f"uncertain 应 SURFACE(exit 3)，得 {code}"


# ── 纯函数单测 ─────────────────────────────────────────────────────────────────

def case_derive_surfaces_unit(_d):
    cfg = {"user": {"app_paths": ["apps/web-app"]}, "admin": {"app_paths": ["apps/admin-web"]},
           "api": {"app_paths": ["services/api-server"]}}
    assert vg.derive_required_surfaces(["apps/web-app/a.tsx"], cfg) == {"user"}
    assert vg.derive_required_surfaces(["apps/admin-web/a.tsx", "services/api-server/b.rs"], cfg) == {"admin", "api"}
    assert vg.derive_required_surfaces(["docs/x.md"], cfg) == set()
    # 前缀不能误命中（app_paths 边界）
    assert vg.derive_required_surfaces(["apps/web-app-other/a.tsx"], cfg) == set()


def case_schema_rejects_malformed(_d):
    assert vg.validate_plan_doc({"journeys": []}), "空 journeys 应被拒"
    assert vg.validate_plan_doc({"journeys": [{"id": "m", "steps": []}]}), "空 steps 应被拒"
    assert vg.validate_plan_doc({"journeys": [{"id": "m", "steps": [{"surface": "user", "action": "x"}]}]}), \
        "没有任何步骤证 AC 应被拒（旅程无检查点）"
    assert not vg.validate_plan_doc({"journeys": [{"id": "m", "steps": [
        {"surface": "user", "action": "x", "proves_acs": ["A"]}]}]})
    assert vg.validate_judge_doc({"verdicts": [{"ac_id": "A", "verdict": "maybe", "evidence_refs": []}]}), "非法 verdict 应被拒"
    # 真实旅程：UI 步缺截图被拒；API 步用响应可过
    assert vg.validate_journey({"journey_id": "m", "steps": [{"surface": "user", "action": "x"}]}), "UI 步缺截图应被拒"
    assert not vg.validate_journey({"journey_id": "m", "steps": [
        {"surface": "api", "action": "POST", "network": [{"target": "api"}]}]}), "API 步带 network 应可过"


CASES = [
    case_pure_ui_passes,
    case_pure_api_passes,
    case_cross_end_toggle_passes,
    case_missing_p0_ac_fails,
    case_cross_end_skips_admin_fails,
    case_feature_surface_undeclared_fails,
    case_toggle_only_on_fails,
    case_forged_judge_ref_fails,
    case_judge_ref_other_ac_step_fails,
    case_tampered_artifact_fails,
    case_step_missing_screenshot_fails,
    case_uncertain_surfaces,
    case_derive_surfaces_unit,
    case_schema_rejects_malformed,
]


def main():
    failures = []
    for case in CASES:
        with tempfile.TemporaryDirectory() as d:
            try:
                case(d)
                print(f"  [PASS] {case.__name__}")
            except AssertionError as e:
                print(f"  [FAIL] {case.__name__}: {e}")
                failures.append(case.__name__)
            except Exception as e:
                print(f"  [ERROR] {case.__name__}: {type(e).__name__}: {e}")
                failures.append(case.__name__)
    print()
    if failures:
        print(f"❌ {len(failures)}/{len(CASES)} 失败: {', '.join(failures)}")
        sys.exit(1)
    print(f"✅ all {len(CASES)} verify_gate cases passed")


if __name__ == "__main__":
    main()
