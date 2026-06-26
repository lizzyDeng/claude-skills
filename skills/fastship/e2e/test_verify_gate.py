#!/usr/bin/env python3
"""
verify_gate.py 回归测试 —— 钉死 spec §13 的八条可证伪验收。

跑法: python3 test_verify_gate.py
纯函数直测 + 子进程跑 CLI 验退出码（0=PASS / 1=FAIL / 3=SURFACE）。
真实 temp 文件 + manifest（截图是真字节、sha256 真算），保证 ④ 证据真实 检查被真正执行。
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

def _write_png(path, content=b"\x89PNG\r\n\x1a\nFAKE-IMAGE-BYTES"):
    with open(path, "wb") as f:
        f.write(content)
    return content


def _state(d, name, *, screenshot=True, network=None, dom=None, png=b"\x89PNGdefault"):
    """造一个 state，写真截图文件，返回 (state_dict, {path: sha256})。"""
    st = {}
    manifest = {}
    if screenshot:
        sp = os.path.join(d, f"{name}.png")
        _write_png(sp, png)
        st["screenshots"] = [sp]
        manifest[sp] = hashlib.sha256(png).hexdigest()
    if network:
        st["network"] = network
    if dom:
        st["dom_facts"] = dom
    return st, manifest


def _bundle(d, ac_id, *, states, journey):
    return {"ac_id": ac_id, "surfaces_touched": [], "states": states, "realized_journey": journey}


def _scenario(d, *, plan_intents, bundles, verdicts, manifest, surfaces=None, changed=None):
    """落盘一套完整 scenario，返回跑 CLI 所需路径。"""
    plan = {"feature": "t", "intents": plan_intents}
    judge = {"verdicts": verdicts}
    with open(os.path.join(d, "verification-plan.json"), "w") as f:
        json.dump(plan, f)
    with open(os.path.join(d, "verify-judge.json"), "w") as f:
        json.dump(judge, f)
    for b in bundles:
        with open(os.path.join(d, f"{b['ac_id']}.bundle.json"), "w") as f:
            json.dump(b, f)
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
    bundles = vg._load_bundles(paths["evidence_dir"])
    manifest = (json.load(open(os.path.join(paths["evidence_dir"], "evidence-manifest.json"))) or {}).get("artifacts") or {}
    surfaces = json.load(open(paths["surfaces"]))
    return vg.evaluate(plan, bundles, judge, surfaces, paths["changed"], manifest, paths["evidence_dir"])


# ── 八条验收 ──────────────────────────────────────────────────────────────────

def case_pure_ui_passes(d):
    """§13.1 纯 UI feature 走完，截图证据，结构 gate PASS（不数 turn）。"""
    st, man = _state(d, "ac1", network=[{"target": "user", "method": "POST", "url": "/api/gift", "status": 201}])
    b = _bundle(d, "AC-1", states={"default": st},
                journey=[{"target": "user", "action": "点礼物入口", "ok": True, "elapsed_ms": 120}])
    p = _scenario(d, plan_intents=[{"ac_id": "AC-1", "assertion": "送礼出现感谢", "required_surfaces": ["user"],
                                    "entry": "/chat", "goal": "送礼", "success_evidence": ["感谢气泡"]}],
                  bundles=[b],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": st["screenshots"][0], "fact": "感谢气泡"}],
                             "reason": "截图+network 双证"}],
                  manifest=man, changed=["apps/web-app/src/Gift.tsx"])
    code, _ = _run_cli(p)
    assert code == 0, f"纯 UI 应 PASS(exit 0)，得 {code}"
    assert _eval(p)["min_turns_used"] is False, "§13.8 min_turns 不得参与判定"


def case_pure_api_passes(d):
    """§13.2 后端 API feature 走 http 证据，无截图，PASS。"""
    st = {"network": [{"target": "api", "method": "POST", "url": "/api/credits", "status": 200}],
          "api_responses": [{"ok": True}]}
    b = _bundle(d, "AC-1", states={"default": st},
                journey=[{"target": "api", "action": "POST /api/credits", "ok": True, "elapsed_ms": 30}])
    p = _scenario(d, plan_intents=[{"ac_id": "AC-1", "assertion": "积分+10", "required_surfaces": ["api"],
                                    "entry": "/api/credits", "goal": "发放积分", "success_evidence": ["200"]}],
                  bundles=[b],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": "network", "fact": "200"}], "reason": "响应 200"}],
                  manifest={}, changed=["services/api-server/src/credits.rs"])
    code, _ = _run_cli(p)
    assert code == 0, f"纯 API 应 PASS，得 {code}"


def case_cross_end_toggle_passes(d):
    """§13.3 cross-端 toggle：admin+user 两表面、ON/OFF 两态出证，PASS。"""
    on, m_on = _state(d, "on", network=[{"target": "user", "method": "GET", "url": "/chat", "status": 200}], png=b"ON-PNG")
    off, m_off = _state(d, "off", png=b"OFF-PNG")
    man = {**m_on, **m_off}
    b = _bundle(d, "AC-1", states={"on": on, "off": off},
                journey=[{"target": "admin", "action": "置 ON 保存", "ok": True, "elapsed_ms": 200},
                         {"target": "user", "action": "验入口可见", "ok": True, "elapsed_ms": 90},
                         {"target": "admin", "action": "置 OFF 保存", "ok": True, "elapsed_ms": 180},
                         {"target": "user", "action": "验入口消失", "ok": True, "elapsed_ms": 80}])
    p = _scenario(d, plan_intents=[{"ac_id": "AC-1", "assertion": "ON 现 OFF 隐", "required_surfaces": ["admin", "user"],
                                    "entry": "/config", "goal": "切开关", "success_evidence": ["ON 现", "OFF 隐"],
                                    "differential": {"flag": "gift_entry", "on_state": "入口可见", "off_state": "入口消失"}}],
                  bundles=[b],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": on["screenshots"][0], "fact": "ON 入口在"},
                                               {"artifact": off["screenshots"][0], "fact": "OFF 入口没"}],
                             "reason": "ON/OFF 对照成立"}],
                  manifest=man, changed=["apps/admin-web/src/Config.tsx", "apps/web-app/src/Chat.tsx"])
    code, out = _run_cli(p)
    assert code == 0, f"cross-端 toggle 应 PASS，得 {code}\n{out[-600:]}"


def case_missing_p0_ac_fails(d):
    """§13.4 故意漏验一条 P0 AC → AC 覆盖 FAIL。"""
    st, man = _state(d, "ac1")
    b = _bundle(d, "AC-1", states={"default": st},
                journey=[{"target": "user", "action": "x", "ok": True, "elapsed_ms": 10}])
    p = _scenario(d, plan_intents=[
                      {"ac_id": "AC-1", "assertion": "a", "required_surfaces": ["user"], "entry": "/", "goal": "g", "success_evidence": ["e"]},
                      {"ac_id": "AC-2", "assertion": "b", "required_surfaces": ["user"], "entry": "/", "goal": "g", "success_evidence": ["e"]}],
                  bundles=[b],  # 只有 AC-1
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": st["screenshots"][0], "fact": "f"}], "reason": "r"}],
                  manifest=man, changed=["apps/web-app/x.tsx"])
    code, _ = _run_cli(p)
    assert code == 1, f"漏 P0 AC 应 FAIL，得 {code}"
    assert _eval(p)["checks"]["ac_coverage"], "应报 ac_coverage 失败"


def case_cross_end_skips_admin_fails(d):
    """§13.5 cross-端旅程只点用户端没碰 admin → surface 覆盖 FAIL。"""
    st, man = _state(d, "ac1")
    b = _bundle(d, "AC-1", states={"default": st},
                journey=[{"target": "user", "action": "只点用户端", "ok": True, "elapsed_ms": 10}])
    p = _scenario(d, plan_intents=[{"ac_id": "AC-1", "assertion": "a", "required_surfaces": ["admin", "user"],
                                    "entry": "/", "goal": "g", "success_evidence": ["e"]}],
                  bundles=[b],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": st["screenshots"][0], "fact": "f"}], "reason": "r"}],
                  manifest=man, changed=["apps/admin-web/c.tsx", "apps/web-app/u.tsx"])
    code, _ = _run_cli(p)
    assert code == 1, f"没碰 admin 应 FAIL，得 {code}"
    assert _eval(p)["checks"]["surface_coverage"], "应报 surface_coverage 失败"


def case_feature_surface_undeclared_fails(d):
    """§13.5 feature 级：diff 改了 admin 但没有 AC 声明覆盖 admin → FAIL。"""
    st, man = _state(d, "ac1")
    b = _bundle(d, "AC-1", states={"default": st},
                journey=[{"target": "user", "action": "x", "ok": True, "elapsed_ms": 10}])
    p = _scenario(d, plan_intents=[{"ac_id": "AC-1", "assertion": "a", "required_surfaces": ["user"],
                                    "entry": "/", "goal": "g", "success_evidence": ["e"]}],
                  bundles=[b],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": st["screenshots"][0], "fact": "f"}], "reason": "r"}],
                  manifest=man, changed=["apps/admin-web/c.tsx", "apps/web-app/u.tsx"])  # 改了 admin 却无 AC 声明
    assert _eval(p)["checks"]["surface_coverage"], "改了 admin 无 AC 覆盖应报 surface_coverage 失败"


def case_toggle_only_on_fails(d):
    """§13.6 toggle 只验 ON 没验 OFF → differential FAIL。"""
    on, man = _state(d, "on")
    b = _bundle(d, "AC-1", states={"on": on},  # 缺 off
                journey=[{"target": "admin", "action": "置 ON", "ok": True, "elapsed_ms": 100},
                         {"target": "user", "action": "验 ON", "ok": True, "elapsed_ms": 90}])
    p = _scenario(d, plan_intents=[{"ac_id": "AC-1", "assertion": "ON/OFF", "required_surfaces": ["admin", "user"],
                                    "entry": "/", "goal": "g", "success_evidence": ["e"],
                                    "differential": {"flag": "f", "on_state": "on", "off_state": "off"}}],
                  bundles=[b],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": on["screenshots"][0], "fact": "f"}], "reason": "r"}],
                  manifest=man, changed=["apps/admin-web/c.tsx", "apps/web-app/u.tsx"])
    code, _ = _run_cli(p)
    assert code == 1, f"只验 ON 应 FAIL，得 {code}"
    assert _eval(p)["checks"]["differential"], "应报 differential 失败"


def case_forged_judge_ref_fails(d):
    """§13.7 裁判伪造 pass 但 evidence_ref 指向不存在文件 → 反橡皮图章 FAIL。"""
    st, man = _state(d, "ac1")
    b = _bundle(d, "AC-1", states={"default": st},
                journey=[{"target": "user", "action": "x", "ok": True, "elapsed_ms": 10}])
    p = _scenario(d, plan_intents=[{"ac_id": "AC-1", "assertion": "a", "required_surfaces": ["user"],
                                    "entry": "/", "goal": "g", "success_evidence": ["e"]}],
                  bundles=[b],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": os.path.join(d, "ghost.png"), "fact": "我看到了"}],
                             "reason": "我说通过就通过"}],  # ghost.png 不存在且不属于本 AC
                  manifest=man, changed=["apps/web-app/x.tsx"])
    code, _ = _run_cli(p)
    assert code == 1, f"裁判伪造引用应 FAIL，得 {code}"
    assert _eval(p)["checks"]["judge_refs"], "应报 judge_refs 失败"


def case_tampered_artifact_fails(d):
    """④ artifact 采集后被改（sha256 不符 manifest）→ FAIL。"""
    st, man = _state(d, "ac1")
    # 篡改截图文件，但 manifest 仍存旧 hash
    with open(st["screenshots"][0], "wb") as f:
        f.write(b"TAMPERED-DIFFERENT-BYTES")
    b = _bundle(d, "AC-1", states={"default": st},
                journey=[{"target": "user", "action": "x", "ok": True, "elapsed_ms": 10}])
    p = _scenario(d, plan_intents=[{"ac_id": "AC-1", "assertion": "a", "required_surfaces": ["user"],
                                    "entry": "/", "goal": "g", "success_evidence": ["e"]}],
                  bundles=[b],
                  verdicts=[{"ac_id": "AC-1", "verdict": "pass",
                             "evidence_refs": [{"artifact": st["screenshots"][0], "fact": "f"}], "reason": "r"}],
                  manifest=man, changed=["apps/web-app/x.tsx"])
    assert _eval(p)["checks"]["evidence_real"], "篡改 artifact 应报 evidence_real 失败"


def case_uncertain_surfaces(d):
    """⑥ 任一 verdict=uncertain → SURFACE（exit 3，阻断等人）。"""
    st, man = _state(d, "ac1")
    b = _bundle(d, "AC-1", states={"default": st},
                journey=[{"target": "user", "action": "x", "ok": True, "elapsed_ms": 10}])
    p = _scenario(d, plan_intents=[{"ac_id": "AC-1", "assertion": "a", "required_surfaces": ["user"],
                                    "entry": "/", "goal": "g", "success_evidence": ["e"]}],
                  bundles=[b],
                  verdicts=[{"ac_id": "AC-1", "verdict": "uncertain",
                             "evidence_refs": [{"artifact": st["screenshots"][0], "fact": "看不太清"}],
                             "reason": "截图模糊"}],
                  manifest=man, changed=["apps/web-app/x.tsx"])
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
    assert vg.validate_plan_doc({"intents": []}), "空 intents 应被拒"
    assert vg.validate_plan_doc({"intents": [{"ac_id": "A"}]}), "缺 assertion/surfaces 应被拒"
    assert not vg.validate_plan_doc({"intents": [{"ac_id": "A", "assertion": "x", "required_surfaces": ["user"]}]})
    assert vg.validate_judge_doc({"verdicts": [{"ac_id": "A", "verdict": "maybe", "evidence_refs": []}]}), "非法 verdict 应被拒"
    assert vg.validate_bundle({"ac_id": "A", "states": {"default": {}}, "realized_journey": [{"action": "x"}]}), "journey 缺 target 应被拒"


CASES = [
    case_pure_ui_passes,
    case_pure_api_passes,
    case_cross_end_toggle_passes,
    case_missing_p0_ac_fails,
    case_cross_end_skips_admin_fails,
    case_feature_surface_undeclared_fails,
    case_toggle_only_on_fails,
    case_forged_judge_ref_fails,
    case_tampered_artifact_fails,
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
