#!/usr/bin/env python3
"""
Verify Gate — AC 驱动旅程验证的确定性结构 gate（替代 e2e_gate.py 的「数 turn」）。

设计见 docs/superpowers/specs/2026-06-26-fastship-ac-driven-verification-design.md §5.5。

验证单元是 AC，不是 turn。本 gate **纯确定性**，不读图、不做语义判断——它只验
「计划/证据/裁判三者对每条 AC 是否结构齐全、证据是否真实、裁判引用是否落到真 artifact」。
看图推理由独立对抗裁判（3.4 第一段，verify-judge.json）完成；本 gate 复核裁判的引用，
封掉橡皮图章。

六道检查（§5.5）：
  ① AC 覆盖   —— 每条 AC 同时在 计划 ∩ 证据 ∩ 裁判，缺一 FAIL
  ② surface 覆盖 —— 每条 AC 的 required_surfaces 在真实走过的 journey/network 里有佐证；
                    且 feature diff 改过的 surface 必须被至少一条 AC 声明覆盖
  ③ differential —— 被标 differential 的 AC，证据含 ON/OFF 两态 + 裁判分别引用两态
  ④ 证据真实   —— 每个 artifact 文件存在、非空、sha256 匹配 manifest（防篡改）
  ⑤ 裁判引用有效 —— 裁判每条 evidence_ref 指向该 AC 真实存在且非空的 artifact / 真实 inline 事实
  ⑥ 派生终判   —— 全 pass + ①-⑤ → PASS；任一 fail 或 ①-⑤ 不过 → FAIL；任一 uncertain → SURFACE

退出码:
  0 = GATE PASS（可自动过，报告留人过目）
  1 = GATE FAIL（进 loop record）
  3 = SURFACE（证据弱/不确定，阻断等人确认 done --verify-confirmed）

用法:
  python3 verify_gate.py --plan verification-plan.json --evidence-dir .claude/fastship-verify \\
      --judge verify-judge.json [--surfaces-config <json>] [--changed-files f1,f2] [-o result.json]
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys


# ─────────────────────────────────────────────────────────────────────────────
# JSON 契约形状（文档即契约）
#
# verification-plan.json
#   { "feature": str, "generated_from": {...}, "intents": [ INTENT ] }
#   INTENT = { "ac_id": str, "assertion": str, "required_surfaces": [str],
#              "entry": str, "goal": str, "success_evidence": [str],
#              "hints_from_diff": dict?, "differential": null | {flag,on_state,off_state} }
#
# evidence bundle: <evidence_dir>/<ac_id>.bundle.json
#   BUNDLE = { "ac_id": str, "surfaces_touched": [str],
#              "states": { STATE_NAME: STATE },        # 非差分 {"default":..}; 差分 {"on":..,"off":..}
#              "realized_journey": [ {target,action,ok,elapsed_ms} ], "raw_log": path? }
#   STATE  = { "screenshots":[path], "dom_facts":[{selector_intent,present}],
#              "network":[{target,method,url,status}], "a11y_snapshot": path?, "api_responses":[..]? }
#
# evidence-manifest.json: <evidence_dir>/evidence-manifest.json
#   { "artifacts": { "<path>": "<sha256>" } }          # 每个文件 artifact 在采集时登记
#
# verify-judge.json
#   { "verdicts": [ {ac_id, verdict:"pass|fail|uncertain", evidence_refs:[{artifact,fact}], reason} ] }
# ─────────────────────────────────────────────────────────────────────────────

VALID_VERDICTS = {"pass", "fail", "uncertain"}
# 裁判 evidence_ref.artifact 允许的伪引用（指 bundle 内 inline 事实，而非文件）
PSEUDO_REFS = {"network", "dom", "dom_facts", "api", "api_responses", "a11y", "a11y_snapshot"}
UI_SURFACES = {"ui", "user", "admin", "web"}  # 需要截图佐证的表面族（启发，可被 config 覆盖）


# ── 通用 ──────────────────────────────────────────────────────────────────────

def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _norm(p):
    return (p or "").replace("\\", "/")


def _resolve(path, base_dir):
    """artifact 路径解析：绝对→原样；否则先 base_dir 再 cwd。返回 abspath（不保证存在）。"""
    if not path:
        return ""
    if os.path.isabs(path):
        return os.path.realpath(path)
    cand = os.path.join(base_dir, path)
    if os.path.exists(cand):
        return os.path.realpath(cand)
    return os.path.realpath(path)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── schema 校验（结构合法性，先于语义检查）────────────────────────────────────

def validate_plan_doc(plan):
    errs = []
    if not isinstance(plan, dict):
        return ["verification-plan 必须是 JSON object"]
    intents = plan.get("intents")
    if not isinstance(intents, list) or not intents:
        return ["verification-plan 缺少非空 intents 数组"]
    seen = set()
    for i, it in enumerate(intents):
        if not isinstance(it, dict):
            errs.append(f"intents[{i}] 非 object")
            continue
        aid = it.get("ac_id")
        if not isinstance(aid, str) or not aid.strip():
            errs.append(f"intents[{i}] 缺少非空 ac_id")
            continue
        if aid in seen:
            errs.append(f"intents 中 ac_id 重复: {aid}")
        seen.add(aid)
        if not isinstance(it.get("assertion"), str) or not it["assertion"].strip():
            errs.append(f"intent {aid} 缺少非空 assertion")
        rs = it.get("required_surfaces")
        if not isinstance(rs, list) or not rs or not all(isinstance(s, str) and s.strip() for s in rs):
            errs.append(f"intent {aid} 缺少非空 required_surfaces（字符串数组）")
        diff = it.get("differential")
        if diff is not None:
            if not isinstance(diff, dict):
                errs.append(f"intent {aid} 的 differential 必须是 object 或 null")
            else:
                for k in ("flag", "on_state", "off_state"):
                    if not isinstance(diff.get(k), str) or not diff[k].strip():
                        errs.append(f"intent {aid} 的 differential 缺少非空 {k}")
    return errs


def validate_bundle(bundle):
    errs = []
    if not isinstance(bundle, dict):
        return ["evidence bundle 必须是 JSON object"]
    aid = bundle.get("ac_id")
    if not isinstance(aid, str) or not aid.strip():
        errs.append("bundle 缺少非空 ac_id")
    states = bundle.get("states")
    if not isinstance(states, dict) or not states:
        errs.append(f"bundle {aid} 缺少非空 states map")
    else:
        for sn, st in states.items():
            if not isinstance(st, dict):
                errs.append(f"bundle {aid} 的 state {sn} 非 object")
    journey = bundle.get("realized_journey")
    if not isinstance(journey, list) or not journey:
        errs.append(f"bundle {aid} 缺少非空 realized_journey（真实走过的路径）")
    else:
        for j, step in enumerate(journey):
            if not isinstance(step, dict):
                errs.append(f"bundle {aid} journey[{j}] 非 object")
            elif not isinstance(step.get("target"), str) or not step["target"].strip():
                errs.append(f"bundle {aid} journey[{j}] 缺少非空 target（哪个表面）")
    return errs


def validate_judge_doc(judge):
    errs = []
    if not isinstance(judge, dict):
        return ["verify-judge 必须是 JSON object"]
    verdicts = judge.get("verdicts")
    if not isinstance(verdicts, list) or not verdicts:
        return ["verify-judge 缺少非空 verdicts 数组"]
    seen = set()
    for i, v in enumerate(verdicts):
        if not isinstance(v, dict):
            errs.append(f"verdicts[{i}] 非 object")
            continue
        aid = v.get("ac_id")
        if not isinstance(aid, str) or not aid.strip():
            errs.append(f"verdicts[{i}] 缺少非空 ac_id")
            continue
        if aid in seen:
            errs.append(f"verdicts 中 ac_id 重复: {aid}")
        seen.add(aid)
        if v.get("verdict") not in VALID_VERDICTS:
            errs.append(f"verdict {aid} 的 verdict 非法（须 pass|fail|uncertain）")
        refs = v.get("evidence_refs")
        if not isinstance(refs, list):
            errs.append(f"verdict {aid} 的 evidence_refs 必须是数组")
    return errs


# ── surface 派生（从 diff 文件路径，可靠）──────────────────────────────────────

def derive_required_surfaces(changed_files, surfaces_config):
    """diff 命中哪个 surface 的 app_paths → 该 surface 成为 feature 级 required。
    纯路径匹配，确定可靠（§6.3①）。changed_files 注入便于单测。"""
    required = set()
    for name, sc in (surfaces_config or {}).items():
        if not isinstance(sc, dict):
            continue
        paths = sc.get("app_paths") or []
        for cf in changed_files or []:
            cfn = _norm(cf)
            for ap in paths:
                apn = _norm(ap).rstrip("/")
                if apn and (cfn == apn or cfn.startswith(apn + "/")):
                    required.add(name)
    return required


# ── 状态/artifact 提取 helper ─────────────────────────────────────────────────

def _state_file_artifacts(state):
    """一个 state 里所有文件型 artifact 路径（截图 + a11y snapshot）。"""
    out = []
    for sp in (state.get("screenshots") or []):
        if isinstance(sp, str) and sp.strip():
            out.append(sp)
    a11y = state.get("a11y_snapshot")
    if isinstance(a11y, str) and a11y.strip():
        out.append(a11y)
    return out


def _bundle_file_artifacts(bundle):
    out = []
    for st in (bundle.get("states") or {}).values():
        if isinstance(st, dict):
            out.extend(_state_file_artifacts(st))
    raw = bundle.get("raw_log")
    if isinstance(raw, str) and raw.strip():
        out.append(raw)
    return out


def _state_has_evidence(state):
    """一个 state 是否含至少一条证据（截图 / DOM 事实 / network）。"""
    if not isinstance(state, dict):
        return False
    return bool(state.get("screenshots") or state.get("dom_facts") or
                state.get("network") or state.get("api_responses"))


def _bundle_surfaces(bundle):
    """真实走过的表面 = journey 成功步骤的 target ∪ 各 state network 的 target。"""
    surfaces = set()
    for step in (bundle.get("realized_journey") or []):
        if isinstance(step, dict) and step.get("ok") and isinstance(step.get("target"), str):
            surfaces.add(step["target"])
    for st in (bundle.get("states") or {}).values():
        if isinstance(st, dict):
            for n in (st.get("network") or []):
                if isinstance(n, dict) and isinstance(n.get("target"), str):
                    surfaces.add(n["target"])
    return surfaces


def _bundle_has_screenshot(bundle):
    for st in (bundle.get("states") or {}).values():
        if isinstance(st, dict) and (st.get("screenshots") or []):
            return True
    return False


# ── 六道结构检查（纯逻辑，返回 failure 字符串列表，空=通过）──────────────────────

def check_ac_coverage(plan_acs, bundle_acs, judge_acs):
    """① 每条 AC 同时在 计划 ∩ 证据 ∩ 裁判。封掉 ac_mapping 声明却没跑的漏洞。"""
    errs = []
    plan, bun, jud = set(plan_acs), set(bundle_acs), set(judge_acs)
    miss_b = sorted(plan - bun)
    miss_j = sorted(plan - jud)
    extra_b = sorted(bun - plan)
    extra_j = sorted(jud - plan)
    if miss_b:
        errs.append(f"以下 AC 在计划中但无证据 bundle（漏验）: {', '.join(miss_b)}")
    if miss_j:
        errs.append(f"以下 AC 在计划中但裁判未出判定: {', '.join(miss_j)}")
    if extra_b:
        errs.append(f"以下证据 bundle 不对应任何计划 AC（漂移）: {', '.join(extra_b)}")
    if extra_j:
        errs.append(f"以下裁判判定不对应任何计划 AC（漂移）: {', '.join(extra_j)}")
    return errs


def check_surface_coverage(intents, bundles, surfaces_config, changed_files):
    """② per-AC：每条 AC 的 required_surfaces 在真实 journey 里有佐证；
       feature 级：diff 改过的 surface 必须被至少一条 AC 声明覆盖（§6.3①、§13.5）。"""
    errs = []
    bundle_by_ac = {b.get("ac_id"): b for b in bundles}
    declared_union = set()
    for it in intents:
        aid = it.get("ac_id")
        req = set(it.get("required_surfaces") or [])
        declared_union |= req
        b = bundle_by_ac.get(aid)
        if not b:
            continue  # AC 覆盖检查会报这条缺 bundle
        actual = _bundle_surfaces(b)
        missing = sorted(req - actual)
        if missing:
            errs.append(f"AC {aid} 需覆盖表面 {sorted(req)}，但旅程只碰到 {sorted(actual)} —— 缺 {missing}")
        # UI 表面要求至少一张截图佐证
        if (req & UI_SURFACES) and not _bundle_has_screenshot(b):
            errs.append(f"AC {aid} 需 UI 表面 {sorted(req & UI_SURFACES)} 却无任何截图佐证")
    # feature 级：diff 改过的表面必须被某条 AC 声明
    derived = derive_required_surfaces(changed_files, surfaces_config)
    uncovered = sorted(derived - declared_union)
    if uncovered:
        errs.append(f"diff 改动了表面 {uncovered}，但没有任何 AC 声明覆盖它 —— "
                    f"cross-端改动缺验证（required-surface 强制）")
    return errs


def check_differential_coverage(intents, bundles, judge):
    """③ 被标 differential 的 AC，证据含 ON/OFF 两态 + 裁判分别引用两态（§6.1、§13.6）。"""
    errs = []
    bundle_by_ac = {b.get("ac_id"): b for b in bundles}
    judge_by_ac = {v.get("ac_id"): v for v in judge.get("verdicts", [])}
    for it in intents:
        if not it.get("differential"):
            continue
        aid = it.get("ac_id")
        b = bundle_by_ac.get(aid)
        if not b:
            continue
        states = b.get("states") or {}
        for need in ("on", "off"):
            st = states.get(need)
            if not isinstance(st, dict) or not _state_has_evidence(st):
                errs.append(f"差分 AC {aid} 缺 {need.upper()} 态证据 —— 只证共存不证因果（differential FAIL）")
        # 裁判须分别引用 on/off 两态的 artifact
        v = judge_by_ac.get(aid)
        if v and v.get("verdict") == "pass":
            refs = {_norm(r.get("artifact")) for r in (v.get("evidence_refs") or [])
                    if isinstance(r, dict) and isinstance(r.get("artifact"), str)}
            on_arts = {_norm(p) for p in _state_file_artifacts(states.get("on") or {})}
            off_arts = {_norm(p) for p in _state_file_artifacts(states.get("off") or {})}
            if on_arts and not (refs & on_arts):
                errs.append(f"差分 AC {aid} 裁判判 pass 但未引用 ON 态任何 artifact")
            if off_arts and not (refs & off_arts):
                errs.append(f"差分 AC {aid} 裁判判 pass 但未引用 OFF 态任何 artifact")
    return errs


def check_evidence_real(bundles, manifest, evidence_dir):
    """④ 每个文件 artifact 存在、非空、sha256 匹配 manifest（防 runner 后篡改）。"""
    errs = []
    resolved_manifest = {}
    for k, v in (manifest or {}).items():
        resolved_manifest[_resolve(k, evidence_dir)] = v
    for b in bundles:
        aid = b.get("ac_id")
        for art in _bundle_file_artifacts(b):
            rp = _resolve(art, evidence_dir)
            if not os.path.exists(rp):
                errs.append(f"AC {aid} 的 artifact 不存在: {art}")
                continue
            try:
                if os.path.getsize(rp) == 0:
                    errs.append(f"AC {aid} 的 artifact 是空文件: {art}")
                    continue
            except OSError:
                errs.append(f"AC {aid} 的 artifact 无法 stat: {art}")
                continue
            if resolved_manifest:
                stored = resolved_manifest.get(rp)
                if stored is None:
                    errs.append(f"AC {aid} 的 artifact 未登记到 manifest（防篡改未覆盖）: {art}")
                elif _sha256(rp) != stored:
                    errs.append(f"AC {aid} 的 artifact sha256 与 manifest 不符（采集后被改）: {art}")
    return errs


def check_judge_refs(bundles, judge):
    """⑤ 裁判每条 evidence_ref 指向该 AC 真实存在且非空的 artifact / 真实 inline 事实。
       pass 判定的空引用、指向不存在文件、指向别的 AC 的 artifact —— 全 FAIL（反橡皮图章，§13.7）。"""
    errs = []
    bundle_by_ac = {b.get("ac_id"): b for b in bundles}
    for v in judge.get("verdicts", []):
        aid = v.get("ac_id")
        verdict = v.get("verdict")
        refs = v.get("evidence_refs") or []
        b = bundle_by_ac.get(aid)
        if verdict == "pass" and not refs:
            errs.append(f"AC {aid} 裁判判 pass 却零 evidence_ref（不许无证自证）")
            continue
        if not b:
            continue  # AC 覆盖检查报缺 bundle
        own_files = {_norm(p) for p in _bundle_file_artifacts(b)}
        has_network = any((st.get("network") for st in (b.get("states") or {}).values()
                           if isinstance(st, dict)))
        has_dom = any((st.get("dom_facts") for st in (b.get("states") or {}).values()
                       if isinstance(st, dict)))
        has_api = any((st.get("api_responses") for st in (b.get("states") or {}).values()
                       if isinstance(st, dict)))
        for r in refs:
            if not isinstance(r, dict):
                errs.append(f"AC {aid} 的 evidence_ref 非 object")
                continue
            art = r.get("artifact")
            if not isinstance(art, str) or not art.strip():
                errs.append(f"AC {aid} 的 evidence_ref 缺 artifact")
                continue
            key = _norm(art)
            low = key.lower()
            if low in PSEUDO_REFS:
                ok = (low in ("network",) and has_network) or \
                     (low in ("dom", "dom_facts") and has_dom) or \
                     (low in ("api", "api_responses") and has_api) or \
                     (low in ("a11y", "a11y_snapshot"))
                if not ok:
                    errs.append(f"AC {aid} 裁判引用了伪证据 '{art}' 但 bundle 无对应 inline 事实")
                continue
            # 文件型引用：必须是本 AC 的 artifact
            if key not in own_files:
                errs.append(f"AC {aid} 裁判引用了不属于本 AC 的 artifact（或凭空捏造）: {art}")
    return errs


# ── 终判 ──────────────────────────────────────────────────────────────────────

STRUCT_CHECK_KEYS = ("ac_coverage", "surface_coverage", "differential", "evidence_real", "judge_refs")


def derive_decision(verdicts, checks):
    """⑥ 自结构派生终判。结构检查（①-⑤）任一不过或任一 verdict=fail → fail；
       否则任一 uncertain → surface；否则 pass。"""
    struct_ok = all(not checks.get(k) for k in STRUCT_CHECK_KEYS)
    vset = [v.get("verdict") for v in verdicts]
    if not struct_ok or any(x == "fail" for x in vset):
        return "fail"
    if any(x == "uncertain" for x in vset):
        return "surface"
    return "pass"


def evaluate(plan, bundles, judge, surfaces_config, changed_files, manifest, evidence_dir):
    """组装六道检查 → 结果 dict。纯逻辑入口（schema 校验由调用方先做或合并）。"""
    intents = plan.get("intents", [])
    plan_acs = [it.get("ac_id") for it in intents if isinstance(it, dict)]
    bundle_acs = [b.get("ac_id") for b in bundles]
    judge_acs = [v.get("ac_id") for v in judge.get("verdicts", [])]

    checks = {
        "ac_coverage": check_ac_coverage(plan_acs, bundle_acs, judge_acs),
        "surface_coverage": check_surface_coverage(intents, bundles, surfaces_config, changed_files),
        "differential": check_differential_coverage(intents, bundles, judge),
        "evidence_real": check_evidence_real(bundles, manifest, evidence_dir),
        "judge_refs": check_judge_refs(bundles, judge),
    }
    verdicts = judge.get("verdicts", [])
    decision = derive_decision(verdicts, checks)

    counts = {"pass": 0, "fail": 0, "uncertain": 0}
    for v in verdicts:
        vd = v.get("verdict")
        if vd in counts:
            counts[vd] += 1
    return {
        "decision": decision,
        "checks": checks,
        "verdicts": verdicts,
        "summary": {
            "total_acs": len(plan_acs),
            "verdict_counts": counts,
            "required_surfaces_derived": sorted(derive_required_surfaces(changed_files, surfaces_config)),
        },
        "min_turns_used": False,  # §13.8 — 显式声明 turn 不参与判定
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _load_bundles(evidence_dir):
    bundles = []
    if not os.path.isdir(evidence_dir):
        return bundles
    for fn in sorted(os.listdir(evidence_dir)):
        if fn.endswith(".bundle.json"):
            try:
                bundles.append(_load_json(os.path.join(evidence_dir, fn)))
            except Exception as e:
                bundles.append({"_load_error": f"{fn}: {e}"})
    return bundles


def _load_surfaces_config(path):
    if path:
        try:
            return _load_json(path)
        except Exception:
            return {}
    # 从项目配置 .claude/fastship.project.json 的 verify.surfaces 读
    try:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
        cfg = _load_json(os.path.join(root, ".claude", "fastship.project.json"))
        return (cfg.get("verify") or {}).get("surfaces") or {}
    except Exception:
        return {}


def _git_changed_files():
    try:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
        out = set()
        for args in (["diff", "--name-only", "HEAD"], ["diff", "--name-only", "--cached"]):
            r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                if line.strip():
                    out.add(line.strip())
        return sorted(out)
    except Exception:
        return []


def _print_raw_evidence(plan, bundles, judge):
    """像 e2e_gate.py 一样把原始证据摊给用户看，别只信裁判总结。"""
    print("\n" + "=" * 64)
    print("原始证据（用户审查用 —— 对照截图/network 判断裁判是否诚实）")
    print("=" * 64)
    judge_by_ac = {v.get("ac_id"): v for v in judge.get("verdicts", [])}
    for it in plan.get("intents", []):
        aid = it.get("ac_id")
        print(f"\n### {aid}  {it.get('assertion', '')}")
        b = next((x for x in bundles if x.get("ac_id") == aid), None)
        if not b:
            print("    ❌ 无证据 bundle")
        else:
            for step in (b.get("realized_journey") or []):
                mark = "✓" if step.get("ok") else "✗"
                print(f"    [{mark}] {step.get('target')}: {step.get('action')} ({step.get('elapsed_ms', 0)}ms)")
            for sn, st in (b.get("states") or {}).items():
                if not isinstance(st, dict):
                    continue
                shots = st.get("screenshots") or []
                nets = st.get("network") or []
                doms = st.get("dom_facts") or []
                print(f"    · state[{sn}]: {len(shots)} 截图, {len(nets)} network, {len(doms)} DOM 事实")
                for n in nets:
                    print(f"        {n.get('method', '?')} {n.get('url', '?')} → {n.get('status', '?')} @{n.get('target', '?')}")
        v = judge_by_ac.get(aid)
        if v:
            print(f"    ⚖️  裁判: {v.get('verdict', '?').upper()} — {v.get('reason', '')}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verify Gate — AC 驱动旅程验证结构 gate")
    ap.add_argument("--plan", required=True, help="verification-plan.json")
    ap.add_argument("--evidence-dir", required=True, help="证据 bundle + manifest 目录")
    ap.add_argument("--judge", required=True, help="verify-judge.json")
    ap.add_argument("--surfaces-config", default=None, help="surfaces 配置 JSON（默认读项目配置）")
    ap.add_argument("--changed-files", default=None, help="逗号分隔的改动文件（默认 git 推导）")
    ap.add_argument("-o", "--output", default=None, help="结果 JSON 输出路径")
    args = ap.parse_args(argv)

    print("=" * 64)
    print("VERIFY GATE — AC 驱动旅程验证（确定性结构检查）")
    print("=" * 64)

    # 加载
    try:
        plan = _load_json(args.plan)
        judge = _load_json(args.judge)
    except Exception as e:
        print(f"\n❌ BLOCKED: 计划/裁判文件加载失败: {e}")
        return 1
    bundles = _load_bundles(args.evidence_dir)
    manifest_path = os.path.join(args.evidence_dir, "evidence-manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        try:
            manifest = (_load_json(manifest_path) or {}).get("artifacts") or {}
        except Exception:
            manifest = {}

    # schema 校验先行
    schema_errs = validate_plan_doc(plan) + validate_judge_doc(judge)
    for b in bundles:
        if "_load_error" in b:
            schema_errs.append(f"bundle 加载失败: {b['_load_error']}")
        else:
            schema_errs += validate_bundle(b)
    if schema_errs:
        print("\n❌ BLOCKED: schema 校验失败")
        for e in schema_errs:
            print(f"   - {e}")
        return 1

    surfaces_config = _load_surfaces_config(args.surfaces_config)
    if args.changed_files is not None:
        changed = [x.strip() for x in args.changed_files.split(",") if x.strip()]
    else:
        changed = _git_changed_files()

    result = evaluate(plan, bundles, judge, surfaces_config, changed, manifest, args.evidence_dir)

    _print_raw_evidence(plan, bundles, judge)

    # 检查结论
    print("\n" + "=" * 64)
    print("结构检查（六道）")
    print("=" * 64)
    any_fail = False
    for key in STRUCT_CHECK_KEYS:
        fails = result["checks"][key]
        if fails:
            any_fail = True
            print(f"\n❌ {key}:")
            for f in fails:
                print(f"   - {f}")
        else:
            print(f"✅ {key}")

    counts = result["summary"]["verdict_counts"]
    print(f"\n裁判: pass={counts['pass']} fail={counts['fail']} uncertain={counts['uncertain']} "
          f"/ 共 {result['summary']['total_acs']} 条 AC")
    print(f"diff 派生必需表面: {result['summary']['required_surfaces_derived'] or '（无）'}")

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 结果写入失败（不阻断）: {e}")

    decision = result["decision"]
    print("\n" + "=" * 64)
    if decision == "pass":
        print("✅ GATE PASS — 全 AC 通过 + 六道结构检查通过。报告留你过目。")
        print("=" * 64)
        return 0
    if decision == "surface":
        print("🟡 SURFACE — 证据弱/不确定，阻断等人确认。看 HTML 报告后 done --verify-confirmed 或判 FAIL。")
        print("=" * 64)
        return 3
    print("❌ GATE FAIL — 结构检查不过或有 AC 判 fail，进 loop record。")
    print("=" * 64)
    return 1


if __name__ == "__main__":
    sys.exit(main())
