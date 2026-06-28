#!/usr/bin/env python3
"""
Verify Gate — AC 驱动【用户旅程】验证的确定性结构 gate（替代 e2e_gate.py 的「数 turn」）。

设计见 docs/superpowers/specs/2026-06-26-fastship-ac-driven-verification-design.md §5.5。

🔴 验证单元是【一条正常用户路径(journey)】，不是孤立的 AC，更不是 turn。
   验证 = 像真用户那样从头走到尾，【每一步都截图】，AC 是沿途的检查点。
   产出的头号交付是【人读测试报告】：有序点击路径 + 每步截图 + 该步证了哪条 AC。

本 gate **纯确定性**，不读图、不做语义判断——它只验「旅程/证据/裁判三者结构是否齐全、
证据是否真实、裁判引用是否落到真 artifact」。看图推理由独立对抗裁判（3.4 第一段，
verify-judge.json）完成；本 gate 复核裁判的引用，封掉橡皮图章。

六道检查（§5.5，重组到旅程上）：
  ① AC 覆盖   —— 每条锁定 AC 同时被【旅程步骤】证到 ∩ 有真实截图 ∩ 裁判出判定，缺一 FAIL
  ② surface 覆盖 —— feature diff 改过的 surface 必须被旅程【真实走过】（cross-端强制，§13.5）
  ③ differential —— 被标 differential 的旅程，真实走过 ON/OFF 两态(各带截图) + 裁判分别引用两态
  ④ 证据真实   —— 每一步的截图存在、非空、sha256 匹配 manifest（防采集后篡改）
  ⑤ 裁判引用有效 —— 裁判每条 evidence_ref 指向【证该 AC 的那一步】的真实截图 / 真实 inline 事实
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
import subprocess
import sys


# ─────────────────────────────────────────────────────────────────────────────
# JSON 契约形状（文档即契约）
#
# verification-plan.json  —— 验证意图 = 一条(或几条)正常用户路径脚本
#   { "feature": str, "generated_from": {...}, "journeys": [ JOURNEY ] }
#   JOURNEY = { "id": str, "title": str,
#               "differential": null | {flag, on_state, off_state},   # 整条旅程是否走两态
#               "steps": [ PLAN_STEP ] }
#   PLAN_STEP = { "no": int, "surface": str, "action": str,     # open/click/type/observe/toggle
#                 "intent": str, "expect": str,                 # 用户做什么 / 应看到什么
#                 "proves_acs": [str],                          # 这步顺带证哪条 AC（导航步可空）
#                 "hints": dict? }                              # 语义定位提示(role/text/label/testid)
#
# 真实旅程证据: <evidence_dir>/<journey_id>.journey.json   —— 真实走过的有序时间线
#   { "journey_id": str, "title": str, "steps": [ REAL_STEP ] }
#   REAL_STEP = { "no": int, "surface": str, "state": "default|on|off",
#                 "action": str, "ok": bool, "screenshot": str,   # 🔴 每一步都必须有一张截图
#                 "caption": str,                                 # 人读：这步用户看到/做了什么
#                 "proves_acs": [str],
#                 "dom_facts":[..]?, "network":[{target,method,url,status}]?,
#                 "api_responses":[..]?, "a11y_snapshot": path?, "elapsed_ms": int? }
#
# evidence-manifest.json: <evidence_dir>/evidence-manifest.json
#   { "artifacts": { "<path>": "<sha256>" } }          # 每个截图在采集当下登记
#
# verify-judge.json
#   { "verdicts": [ {ac_id, verdict:"pass|fail|uncertain", evidence_refs:[{artifact,fact}], reason} ] }
# ─────────────────────────────────────────────────────────────────────────────

VALID_VERDICTS = {"pass", "fail", "uncertain"}
VALID_STATES = {"default", "on", "off"}
# 裁判 evidence_ref.artifact 允许的伪引用（指步骤内 inline 事实，而非文件）
PSEUDO_REFS = {"network", "dom", "dom_facts", "api", "api_responses", "a11y", "a11y_snapshot"}
# API 表面名（启发）：这些表面的步骤可用 network/响应替代截图；其余表面=UI，【每步必须截图】。
API_SURFACE_HINTS = {"api", "backend", "server", "service", "http", "grpc"}


def _is_api_surface(surface):
    return (surface or "").strip().lower() in API_SURFACE_HINTS


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
    """验证意图：journeys[] 每条是有序用户路径，步骤声明 proves_acs。"""
    errs = []
    if not isinstance(plan, dict):
        return ["verification-plan 必须是 JSON object"]
    journeys = plan.get("journeys")
    if not isinstance(journeys, list) or not journeys:
        return ["verification-plan 缺少非空 journeys 数组（验证单元是用户旅程）"]
    seen = set()
    for i, j in enumerate(journeys):
        if not isinstance(j, dict):
            errs.append(f"journeys[{i}] 非 object")
            continue
        jid = j.get("id")
        if not isinstance(jid, str) or not jid.strip():
            errs.append(f"journeys[{i}] 缺少非空 id")
            continue
        if jid in seen:
            errs.append(f"journeys 中 id 重复: {jid}")
        seen.add(jid)
        steps = j.get("steps")
        if not isinstance(steps, list) or not steps:
            errs.append(f"journey {jid} 缺少非空 steps（用户路径至少一步）")
        else:
            for k, st in enumerate(steps):
                if not isinstance(st, dict):
                    errs.append(f"journey {jid} steps[{k}] 非 object")
                    continue
                if not isinstance(st.get("surface"), str) or not st["surface"].strip():
                    errs.append(f"journey {jid} steps[{k}] 缺少非空 surface（哪个表面）")
                if not isinstance(st.get("action"), str) or not st["action"].strip():
                    errs.append(f"journey {jid} steps[{k}] 缺少非空 action（用户动作）")
                pa = st.get("proves_acs", [])
                if not isinstance(pa, list):
                    errs.append(f"journey {jid} steps[{k}] 的 proves_acs 必须是数组")
        diff = j.get("differential")
        if diff is not None:
            if not isinstance(diff, dict):
                errs.append(f"journey {jid} 的 differential 必须是 object 或 null")
            else:
                for key in ("flag", "on_state", "off_state"):
                    if not isinstance(diff.get(key), str) or not diff[key].strip():
                        errs.append(f"journey {jid} 的 differential 缺少非空 {key}")
    # 至少有一条步骤证到某条 AC（否则旅程不验任何东西）
    if not errs:
        any_ac = any(st.get("proves_acs")
                     for j in journeys for st in (j.get("steps") or [])
                     if isinstance(st, dict))
        if not any_ac:
            errs.append("没有任何步骤声明 proves_acs —— 旅程没有检查点，等于不验")
    return errs


def validate_journey(journey):
    """真实旅程证据：有序步骤，🔴 每一步都必须有一张截图。"""
    errs = []
    if not isinstance(journey, dict):
        return ["journey 证据必须是 JSON object"]
    jid = journey.get("journey_id")
    if not isinstance(jid, str) or not jid.strip():
        errs.append("journey 证据缺少非空 journey_id")
    steps = journey.get("steps")
    if not isinstance(steps, list) or not steps:
        errs.append(f"journey {jid} 缺少非空 steps（真实走过的路径）")
        return errs
    for k, st in enumerate(steps):
        if not isinstance(st, dict):
            errs.append(f"journey {jid} steps[{k}] 非 object")
            continue
        if not isinstance(st.get("surface"), str) or not st["surface"].strip():
            errs.append(f"journey {jid} steps[{k}] 缺少非空 surface")
        if not isinstance(st.get("action"), str) or not st["action"].strip():
            errs.append(f"journey {jid} steps[{k}] 缺少非空 action")
        shot = st.get("screenshot")
        has_shot = isinstance(shot, str) and bool(shot.strip())
        if _is_api_surface(st.get("surface")):
            if not (has_shot or st.get("network") or st.get("api_responses")):
                errs.append(f"journey {jid} steps[{k}]（API 表面）缺证据（network/响应/截图至少一）")
        elif not has_shot:
            errs.append(f"journey {jid} steps[{k}] 缺截图（UI 路径每一步都必须截图）")
        state = st.get("state", "default")
        if state not in VALID_STATES:
            errs.append(f"journey {jid} steps[{k}] 的 state 非法（须 default|on|off）: {state}")
        pa = st.get("proves_acs", [])
        if not isinstance(pa, list):
            errs.append(f"journey {jid} steps[{k}] 的 proves_acs 必须是数组")
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


# ── 旅程/步骤提取 helper ───────────────────────────────────────────────────────

def _plan_journeys(plan):
    return [j for j in (plan.get("journeys") or []) if isinstance(j, dict)]


def _plan_acs(plan):
    """计划侧覆盖的 AC = 所有计划步骤 proves_acs 的并集。"""
    out = set()
    for j in _plan_journeys(plan):
        for st in (j.get("steps") or []):
            if isinstance(st, dict):
                for ac in (st.get("proves_acs") or []):
                    if isinstance(ac, str) and ac.strip():
                        out.add(ac)
    return out


def _journey_steps(journey):
    return [s for s in (journey.get("steps") or []) if isinstance(s, dict)]


def _flat_steps(journeys):
    """所有真实旅程证据的步骤拉平成一条有序流。"""
    out = []
    for j in journeys:
        if isinstance(j, dict):
            out.extend(_journey_steps(j))
    return out


def _step_state(step):
    return step.get("state", "default") or "default"


def _step_ok(step):
    return step.get("ok", True)


def _step_shot(step):
    s = step.get("screenshot")
    return s if (isinstance(s, str) and s.strip()) else None


def _step_has_evidence(step):
    """一步是否含至少一条证据（截图 / DOM / network / 响应）。"""
    return bool(_step_shot(step) or step.get("network") or
                step.get("api_responses") or step.get("dom_facts"))


def _ac_proven(steps):
    """ac_id 集合：被某条【ok 且有证据】的步骤证到（UI 步=截图，API 步=响应/network）。"""
    out = set()
    for st in steps:
        if not _step_ok(st) or not _step_has_evidence(st):
            continue
        for ac in (st.get("proves_acs") or []):
            if isinstance(ac, str) and ac.strip():
                out.add(ac)
    return out


def _step_file_artifacts(step):
    """一步里所有文件型 artifact（截图 + 可选 a11y snapshot）。"""
    out = []
    shot = _step_shot(step)
    if shot:
        out.append(shot)
    a11y = step.get("a11y_snapshot")
    if isinstance(a11y, str) and a11y.strip():
        out.append(a11y)
    return out


def _ac_shots(steps, state=None):
    """ac_id → {规范化截图路径}（仅 ok 且有截图的步骤；可按 state 过滤）。"""
    m = {}
    for st in steps:
        if state is not None and _step_state(st) != state:
            continue
        if not _step_ok(st):
            continue
        shot = _step_shot(st)
        if not shot:
            continue
        for ac in (st.get("proves_acs") or []):
            if isinstance(ac, str) and ac.strip():
                m.setdefault(ac, set()).add(_norm(shot))
    return m


def _ac_files(steps):
    """ac_id → {规范化文件 artifact}（截图 + a11y，证该 AC 的步骤；裁判文件引用须落此集）。"""
    m = {}
    for st in steps:
        if not _step_ok(st):
            continue
        files = {_norm(p) for p in _step_file_artifacts(st)}
        if not files:
            continue
        for ac in (st.get("proves_acs") or []):
            if isinstance(ac, str) and ac.strip():
                m.setdefault(ac, set()).update(files)
    return m


def _ac_inline(steps):
    """ac_id → {network,dom,api}（证该 AC 的步骤是否含对应 inline 事实，裁判伪引用佐证用）。"""
    m = {}
    for st in steps:
        for ac in (st.get("proves_acs") or []):
            if not (isinstance(ac, str) and ac.strip()):
                continue
            d = m.setdefault(ac, {"network": False, "dom": False, "api": False})
            if st.get("network"):
                d["network"] = True
            if st.get("dom_facts"):
                d["dom"] = True
            if st.get("api_responses"):
                d["api"] = True
    return m


def _realized_surfaces(steps):
    """旅程真实走过的表面 = ok 步骤的 surface ∪ 各步 network 的 target。"""
    surfaces = set()
    for st in steps:
        if not _step_ok(st):
            continue
        sf = st.get("surface")
        if isinstance(sf, str) and sf.strip():
            surfaces.add(sf)
        for n in (st.get("network") or []):
            if isinstance(n, dict) and isinstance(n.get("target"), str) and n["target"].strip():
                surfaces.add(n["target"])
    return surfaces


# ── 六道结构检查（纯逻辑，返回 failure 字符串列表，空=通过）──────────────────────

def check_ac_coverage(plan_acs, realized_acs, judge_acs):
    """① 每条 AC 同时在 计划 ∩ 真实旅程(有截图) ∩ 裁判。封掉「声明却没真走」的漏洞。"""
    errs = []
    plan, real, jud = set(plan_acs), set(realized_acs), set(judge_acs)
    miss_r = sorted(plan - real)
    miss_j = sorted(plan - jud)
    extra_r = sorted(real - plan)
    extra_j = sorted(jud - plan)
    if miss_r:
        errs.append(f"以下 AC 在计划中但旅程没有【带截图的步骤】证它（漏验）: {', '.join(miss_r)}")
    if miss_j:
        errs.append(f"以下 AC 在计划中但裁判未出判定: {', '.join(miss_j)}")
    if extra_r:
        errs.append(f"以下 AC 被旅程步骤证到却不在计划里（漂移）: {', '.join(extra_r)}")
    if extra_j:
        errs.append(f"以下裁判判定不对应任何计划 AC（漂移）: {', '.join(extra_j)}")
    return errs


def check_surface_coverage(realized_steps, surfaces_config, changed_files):
    """② feature 级：diff 改过的 surface 必须被旅程【真实走过】（cross-端强制，§6.3①、§13.5）。"""
    errs = []
    derived = derive_required_surfaces(changed_files, surfaces_config)
    if not derived:
        return errs
    actual = _realized_surfaces(realized_steps)
    uncovered = sorted(derived - actual)
    if uncovered:
        errs.append(f"diff 改动了表面 {uncovered}，但用户旅程从未真实走过它 —— "
                    f"cross-端改动缺验证（旅程只走 {sorted(actual)}；required-surface 强制）")
    return errs


def check_differential_coverage(plan, journeys_realized, judge):
    """③ 被标 differential 的旅程，真实走过 ON/OFF 两态(各带截图) + 裁判分别引用两态（§6.1、§13.6）。"""
    errs = []
    realized_by_id = {j.get("journey_id"): j for j in journeys_realized if isinstance(j, dict)}
    judge_by_ac = {v.get("ac_id"): v for v in judge.get("verdicts", []) if isinstance(v, dict)}
    for pj in _plan_journeys(plan):
        if not pj.get("differential"):
            continue
        jid = pj.get("id")
        rj = realized_by_id.get(jid)
        steps = _journey_steps(rj) if rj else []
        for need in ("on", "off"):
            has = any(_step_state(s) == need and _step_ok(s) and _step_has_evidence(s) for s in steps)
            if not has:
                errs.append(f"差分旅程 {jid} 缺 {need.upper()} 态证据 —— 只证共存不证因果（differential FAIL）")
        # 差分旅程证到的 AC，裁判 pass 须分别引用 ON/OFF 两态截图
        diff_acs = set()
        for st in (pj.get("steps") or []):
            if isinstance(st, dict):
                diff_acs.update(a for a in (st.get("proves_acs") or []) if isinstance(a, str) and a.strip())
        on_shots = _ac_shots(steps, "on")
        off_shots = _ac_shots(steps, "off")
        for ac in sorted(diff_acs):
            v = judge_by_ac.get(ac)
            if not (v and v.get("verdict") == "pass"):
                continue
            refs = {_norm(r.get("artifact")) for r in (v.get("evidence_refs") or [])
                    if isinstance(r, dict) and isinstance(r.get("artifact"), str)}
            on_a = on_shots.get(ac, set())
            off_a = off_shots.get(ac, set())
            if on_a and not (refs & on_a):
                errs.append(f"差分 AC {ac} 裁判判 pass 但未引用 ON 态任何截图")
            if off_a and not (refs & off_a):
                errs.append(f"差分 AC {ac} 裁判判 pass 但未引用 OFF 态任何截图")
    return errs


def check_evidence_real(realized_steps, manifest, evidence_dir):
    """④ 每一步的截图存在、非空、sha256 匹配 manifest（防 runner 后篡改）。"""
    errs = []
    resolved_manifest = {}
    for k, v in (manifest or {}).items():
        resolved_manifest[_resolve(k, evidence_dir)] = v
    for st in realized_steps:
        label = f"step#{st.get('no', '?')}@{st.get('surface', '?')}"
        for art in _step_file_artifacts(st):
            rp = _resolve(art, evidence_dir)
            if not os.path.exists(rp):
                errs.append(f"{label} 的截图不存在: {art}")
                continue
            try:
                if os.path.getsize(rp) == 0:
                    errs.append(f"{label} 的截图是空文件: {art}")
                    continue
            except OSError:
                errs.append(f"{label} 的截图无法 stat: {art}")
                continue
            if resolved_manifest:
                stored = resolved_manifest.get(rp)
                if stored is None:
                    errs.append(f"{label} 的截图未登记到 manifest（防篡改未覆盖）: {art}")
                elif _sha256(rp) != stored:
                    errs.append(f"{label} 的截图 sha256 与 manifest 不符（采集后被改）: {art}")
    return errs


def check_judge_refs(realized_steps, judge):
    """⑤ 裁判每条 evidence_ref 指向【证该 AC 的那一步】真实截图 / 真实 inline 事实。
       pass 的空引用、指向别的 AC 的截图、凭空捏造 —— 全 FAIL（反橡皮图章，§13.7）。"""
    errs = []
    ac_files = _ac_files(realized_steps)
    ac_inline = _ac_inline(realized_steps)
    for v in judge.get("verdicts", []):
        if not isinstance(v, dict):
            continue
        aid = v.get("ac_id")
        verdict = v.get("verdict")
        refs = v.get("evidence_refs") or []
        if verdict == "pass" and not refs:
            errs.append(f"AC {aid} 裁判判 pass 却零 evidence_ref（不许无证自证）")
            continue
        own_files = ac_files.get(aid, set())
        inl = ac_inline.get(aid, {"network": False, "dom": False, "api": False})
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
                ok = (low == "network" and inl["network"]) or \
                     (low in ("dom", "dom_facts") and inl["dom"]) or \
                     (low in ("api", "api_responses") and inl["api"]) or \
                     (low in ("a11y", "a11y_snapshot"))
                if not ok:
                    errs.append(f"AC {aid} 裁判引用了伪证据 '{art}' 但证该 AC 的步骤无对应 inline 事实")
                continue
            # 文件型引用：必须是【证本 AC 的步骤】的截图
            if key not in own_files:
                errs.append(f"AC {aid} 裁判引用了不属于该 AC 步骤的截图（或凭空捏造）: {art}")
    return errs


# ── 终判 ──────────────────────────────────────────────────────────────────────

STRUCT_CHECK_KEYS = ("ac_coverage", "surface_coverage", "differential", "evidence_real", "judge_refs")


def derive_decision(verdicts, checks):
    """⑥ 自结构派生终判。结构检查（①-⑤）任一不过或任一 verdict=fail → fail；
       否则任一 uncertain → surface；否则 pass。"""
    struct_ok = all(not checks.get(k) for k in STRUCT_CHECK_KEYS)
    vset = [v.get("verdict") for v in verdicts if isinstance(v, dict)]
    if not struct_ok or any(x == "fail" for x in vset):
        return "fail"
    if any(x == "uncertain" for x in vset):
        return "surface"
    return "pass"


def evaluate(plan, journeys_realized, judge, surfaces_config, changed_files, manifest, evidence_dir):
    """组装六道检查 → 结果 dict。纯逻辑入口（schema 校验由调用方先做或合并）。"""
    plan_acs = _plan_acs(plan)
    realized_steps = _flat_steps(journeys_realized)
    realized_acs = _ac_proven(realized_steps)
    judge_acs = [v.get("ac_id") for v in judge.get("verdicts", []) if isinstance(v, dict)]

    checks = {
        "ac_coverage": check_ac_coverage(plan_acs, realized_acs, judge_acs),
        "surface_coverage": check_surface_coverage(realized_steps, surfaces_config, changed_files),
        "differential": check_differential_coverage(plan, journeys_realized, judge),
        "evidence_real": check_evidence_real(realized_steps, manifest, evidence_dir),
        "judge_refs": check_judge_refs(realized_steps, judge),
    }
    verdicts = [v for v in judge.get("verdicts", []) if isinstance(v, dict)]
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
            "total_journeys": len(journeys_realized),
            "total_steps": len(realized_steps),
            "verdict_counts": counts,
            "required_surfaces_derived": sorted(derive_required_surfaces(changed_files, surfaces_config)),
            "surfaces_walked": sorted(_realized_surfaces(realized_steps)),
        },
        "min_turns_used": False,  # §13.8 — 显式声明 turn 不参与判定
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _load_journeys(evidence_dir):
    journeys = []
    if not os.path.isdir(evidence_dir):
        return journeys
    for fn in sorted(os.listdir(evidence_dir)):
        if fn.endswith(".journey.json"):
            try:
                journeys.append(_load_json(os.path.join(evidence_dir, fn)))
            except Exception as e:
                journeys.append({"_load_error": f"{fn}: {e}"})
    return journeys


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


def _print_raw_evidence(plan, journeys_realized, judge):
    """像 e2e_gate.py 一样把【完整点击路径 + 每步截图】摊给用户看，别只信裁判总结。"""
    print("\n" + "=" * 64)
    print("用户旅程时间线（人读测试路径 —— 对照每步截图判断裁判是否诚实）")
    print("=" * 64)
    plan_titles = {j.get("id"): j.get("title", "") for j in _plan_journeys(plan)}
    for rj in journeys_realized:
        if not isinstance(rj, dict):
            continue
        jid = rj.get("journey_id", "?")
        title = rj.get("title") or plan_titles.get(jid, "")
        print(f"\n▶ 旅程 [{jid}] {title}")
        for st in _journey_steps(rj):
            mark = "✓" if _step_ok(st) else "✗"
            state = _step_state(st)
            tag = f" ({state})" if state != "default" else ""
            acs = ", ".join(st.get("proves_acs") or []) or "—"
            shot = _step_shot(st) or ("(API 步·无截图)" if _is_api_surface(st.get("surface")) else "（缺截图！）")
            print(f"  [{mark}] #{st.get('no', '?')}{tag} @{st.get('surface', '?')} "
                  f"{st.get('action', '?')}: {st.get('caption') or st.get('intent') or ''}")
            print(f"        证 AC: {acs} | 截图: {shot}")
            for n in (st.get("network") or []):
                print(f"        net {n.get('method', '?')} {n.get('url', '?')} → "
                      f"{n.get('status', '?')} @{n.get('target', '?')}")
    print("\n" + "-" * 64)
    print("裁判判定")
    print("-" * 64)
    for v in judge.get("verdicts", []):
        if isinstance(v, dict):
            print(f"  ⚖️  {v.get('ac_id', '?')}: {str(v.get('verdict', '?')).upper()} — {v.get('reason', '')}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verify Gate — AC 驱动用户旅程验证结构 gate")
    ap.add_argument("--plan", required=True, help="verification-plan.json")
    ap.add_argument("--evidence-dir", required=True, help="*.journey.json 证据 + manifest 目录")
    ap.add_argument("--judge", required=True, help="verify-judge.json")
    ap.add_argument("--surfaces-config", default=None, help="surfaces 配置 JSON（默认读项目配置）")
    ap.add_argument("--changed-files", default=None, help="逗号分隔的改动文件（默认 git 推导）")
    ap.add_argument("-o", "--output", default=None, help="结果 JSON 输出路径")
    args = ap.parse_args(argv)

    print("=" * 64)
    print("VERIFY GATE — AC 驱动【用户旅程】验证（确定性结构检查）")
    print("=" * 64)

    # 加载
    try:
        plan = _load_json(args.plan)
        judge = _load_json(args.judge)
    except Exception as e:
        print(f"\n❌ BLOCKED: 计划/裁判文件加载失败: {e}")
        return 1
    journeys = _load_journeys(args.evidence_dir)
    manifest_path = os.path.join(args.evidence_dir, "evidence-manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        try:
            manifest = (_load_json(manifest_path) or {}).get("artifacts") or {}
        except Exception:
            manifest = {}

    # schema 校验先行
    schema_errs = validate_plan_doc(plan) + validate_judge_doc(judge)
    if not journeys:
        schema_errs.append("evidence-dir 下无任何 *.journey.json（3.3 未产出真实旅程证据）")
    for j in journeys:
        if isinstance(j, dict) and "_load_error" in j:
            schema_errs.append(f"journey 加载失败: {j['_load_error']}")
        else:
            schema_errs += validate_journey(j)
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

    result = evaluate(plan, journeys, judge, surfaces_config, changed, manifest, args.evidence_dir)

    _print_raw_evidence(plan, journeys, judge)

    # 检查结论
    print("\n" + "=" * 64)
    print("结构检查（六道）")
    print("=" * 64)
    for key in STRUCT_CHECK_KEYS:
        fails = result["checks"][key]
        if fails:
            print(f"\n❌ {key}:")
            for f in fails:
                print(f"   - {f}")
        else:
            print(f"✅ {key}")

    s = result["summary"]
    counts = s["verdict_counts"]
    print(f"\n旅程: {s['total_journeys']} 条 / {s['total_steps']} 步（每步均截图）")
    print(f"裁判: pass={counts['pass']} fail={counts['fail']} uncertain={counts['uncertain']} "
          f"/ 共 {s['total_acs']} 条 AC")
    print(f"diff 派生必经表面: {s['required_surfaces_derived'] or '（无）'} | 旅程真实走过: {s['surfaces_walked'] or '（无）'}")

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 结果写入失败（不阻断）: {e}")

    decision = result["decision"]
    print("\n" + "=" * 64)
    if decision == "pass":
        print("✅ GATE PASS — 全 AC 通过 + 六道结构检查通过。旅程报告留你过目。")
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
