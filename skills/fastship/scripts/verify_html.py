#!/usr/bin/env python3
"""
verify_html.py — 把 AC 驱动验证的 计划+证据+裁判 渲染成离线自包含 HTML 测试报告。

spec §7。永远生成、不进可信账本（gitignore）、生成失败不阻断。
镜像 scripts/plan_html.py 的自包含单文件套路：截图 base64 内嵌，无外链。

报告内容：
  - 顶部总判（PASS / FAIL / 待确认）+ 各 AC 通过数 + diff 派生必需表面
  - 每条 AC 一卡片：assertion → 人类可读测试路径(realized_journey) → 截图(差分并排 ON/OFF)
    → network/DOM 事实 → 裁判 verdict + reason + 引用证据
  - 失败 / uncertain 的 AC 高亮置顶

用法:
  python3 verify_html.py --plan verification-plan.json --evidence-dir <dir> \\
      --judge verify-judge.json [--gate-result result.json] [-o report.html] [--no-open]
"""

import argparse
import base64
import html as _html
import json
import os
import sys

_CSS = """
:root{--bg:#0f1115;--card:#1a1d24;--ln:#2a2f3a;--tx:#e6e8ee;--mut:#9aa3b2;
--ok:#3fb950;--bad:#f85149;--warn:#d29922;--accent:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--mut);margin-bottom:20px}
.banner{padding:14px 18px;border-radius:10px;font-size:18px;font-weight:600;margin-bottom:8px}
.banner.pass{background:rgba(63,185,80,.14);border:1px solid var(--ok);color:var(--ok)}
.banner.fail{background:rgba(248,81,73,.14);border:1px solid var(--bad);color:var(--bad)}
.banner.surface{background:rgba(210,153,34,.14);border:1px solid var(--warn);color:var(--warn)}
.meta{display:flex;gap:18px;flex-wrap:wrap;color:var(--mut);font-size:13px;margin-bottom:22px}
.meta b{color:var(--tx)}
.card{background:var(--card);border:1px solid var(--ln);border-radius:12px;padding:18px 20px;margin-bottom:16px}
.card.fail{border-color:var(--bad)}
.card.uncertain{border-color:var(--warn)}
.acid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--accent)}
.assert{font-size:16px;font-weight:600;margin:4px 0 12px}
.lbl{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin:14px 0 6px}
.journey{list-style:none;padding:0;margin:0}
.journey li{padding:3px 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.tg{display:inline-block;min-width:54px;color:var(--accent)}
.ok{color:var(--ok)}.no{color:var(--bad)}
.shots{display:flex;gap:12px;flex-wrap:wrap}
.shot{border:1px solid var(--ln);border-radius:8px;overflow:hidden;max-width:340px}
.shot .cap{font-size:11px;color:var(--mut);padding:4px 8px;background:#12151b}
.shot img{display:block;max-width:340px;height:auto}
.facts{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--mut)}
.facts div{padding:1px 0}
.verdict{margin-top:14px;padding:12px 14px;border-radius:8px;background:#12151b;border:1px solid var(--ln)}
.vbadge{font-weight:700;padding:2px 8px;border-radius:5px;font-size:12px}
.vbadge.pass{background:rgba(63,185,80,.16);color:var(--ok)}
.vbadge.fail{background:rgba(248,81,73,.16);color:var(--bad)}
.vbadge.uncertain{background:rgba(210,153,34,.16);color:var(--warn)}
.refs{margin:8px 0 0;padding-left:18px;color:var(--mut);font-size:12.5px}
.checks{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;margin-top:6px}
.checks .c-ok{color:var(--ok)}.checks .c-bad{color:var(--bad)}
"""

MAX_EMBED_BYTES = 4 * 1024 * 1024  # 单图内嵌上限，超过只显示路径，避免报告爆炸


def _esc(s):
    return _html.escape(str(s if s is not None else ""))


def _img_data_uri(path, base_dir):
    rp = path if os.path.isabs(path) else os.path.join(base_dir, path)
    try:
        if not os.path.exists(rp) or os.path.getsize(rp) == 0 or os.path.getsize(rp) > MAX_EMBED_BYTES:
            return None
        ext = os.path.splitext(rp)[1].lower().lstrip(".") or "png"
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif"}.get(ext, "png")
        with open(rp, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/{mime};base64,{b64}"
    except Exception:
        return None


def _load_bundles(evidence_dir):
    out = {}
    if not os.path.isdir(evidence_dir):
        return out
    for fn in sorted(os.listdir(evidence_dir)):
        if fn.endswith(".bundle.json"):
            try:
                with open(os.path.join(evidence_dir, fn), encoding="utf-8") as f:
                    b = json.load(f)
                out[b.get("ac_id")] = b
            except Exception:
                pass
    return out


def _display_decision(gate_result, judge):
    """优先用 gate result.json 的权威 decision；否则从裁判 verdict 推显示用结论。"""
    if gate_result and gate_result.get("decision"):
        return gate_result["decision"]
    vs = [v.get("verdict") for v in judge.get("verdicts", [])]
    if any(x == "fail" for x in vs):
        return "fail"
    if any(x == "uncertain" for x in vs):
        return "surface"
    return "pass" if vs else "surface"


def _render_state(state_name, state, base_dir):
    parts = []
    shots = state.get("screenshots") or []
    imgs = []
    for sp in shots:
        uri = _img_data_uri(sp, base_dir)
        cap = _esc(state_name) + " · " + _esc(os.path.basename(str(sp)))
        if uri:
            imgs.append(f'<div class="shot"><div class="cap">{cap}</div><img src="{uri}"/></div>')
        else:
            imgs.append(f'<div class="shot"><div class="cap">{cap}（未内嵌：{_esc(sp)}）</div></div>')
    if imgs:
        parts.append('<div class="shots">' + "".join(imgs) + "</div>")
    facts = []
    for n in (state.get("network") or []):
        facts.append(f"<div>↳ {_esc(n.get('method', '?'))} {_esc(n.get('url', '?'))} → "
                     f"{_esc(n.get('status', '?'))} @{_esc(n.get('target', '?'))}</div>")
    for dfact in (state.get("dom_facts") or []):
        present = "✓在" if dfact.get("present") else "✗无"
        facts.append(f"<div>↳ DOM {_esc(dfact.get('selector_intent', '?'))}: {present}</div>")
    if facts:
        parts.append('<div class="facts">' + "".join(facts) + "</div>")
    return "".join(parts)


def _render_ac_card(intent, bundle, verdict, base_dir):
    aid = intent.get("ac_id")
    vd = (verdict or {}).get("verdict") or ("missing" if bundle is None else "?")
    cls = "fail" if vd in ("fail", "missing") else ("uncertain" if vd == "uncertain" else "")
    out = [f'<div class="card {cls}">']
    out.append(f'<div class="acid">{_esc(aid)}'
               + (f'  · differential[{_esc(intent["differential"].get("flag"))}]' if intent.get("differential") else "")
               + "</div>")
    out.append(f'<div class="assert">{_esc(intent.get("assertion"))}</div>')

    if bundle is None:
        out.append('<div class="no">❌ 无证据 bundle —— 这条 AC 没被验证</div></div>')
        return "".join(out)

    # 测试路径
    out.append('<div class="lbl">测试路径（真实走过）</div><ul class="journey">')
    for step in (bundle.get("realized_journey") or []):
        mark = '<span class="ok">✓</span>' if step.get("ok") else '<span class="no">✗</span>'
        out.append(f'<li>{mark} <span class="tg">{_esc(step.get("target"))}</span> '
                   f'{_esc(step.get("action"))} <span style="color:#6b7280">({_esc(step.get("elapsed_ms", 0))}ms)</span></li>')
    out.append("</ul>")

    # 各 state 证据（差分两态并排）
    states = bundle.get("states") or {}
    out.append('<div class="lbl">证据</div>')
    for sn in sorted(states.keys(), key=lambda x: {"on": 0, "off": 1, "default": 2}.get(x, 9)):
        st = states[sn]
        if isinstance(st, dict):
            out.append(_render_state(sn, st, base_dir))

    # 裁判
    if verdict:
        out.append(f'<div class="verdict"><span class="vbadge {_esc(vd)}">{_esc(vd).upper()}</span> '
                   f'&nbsp;{_esc(verdict.get("reason"))}')
        refs = verdict.get("evidence_refs") or []
        if refs:
            out.append('<ul class="refs">')
            for r in refs:
                out.append(f'<li>{_esc(r.get("artifact"))} — {_esc(r.get("fact"))}</li>')
            out.append("</ul>")
        out.append("</div>")
    out.append("</div>")
    return "".join(out)


def render_verify_html(plan, bundles, judge, gate_result=None, base_dir="."):
    decision = _display_decision(gate_result, judge)
    banner_txt = {"pass": "✅ GATE PASS — 全 AC 通过", "fail": "❌ GATE FAIL — 有 AC 未通过或结构检查不过",
                  "surface": "🟡 待确认 — 证据弱/不确定，请看截图后确认"}.get(decision, decision)
    judge_by_ac = {v.get("ac_id"): v for v in judge.get("verdicts", [])}
    bundle_by_ac = bundles

    intents = plan.get("intents", [])
    counts = {"pass": 0, "fail": 0, "uncertain": 0}
    for v in judge.get("verdicts", []):
        if v.get("verdict") in counts:
            counts[v["verdict"]] += 1

    # 失败/uncertain 置顶
    def _rank(it):
        vd = (judge_by_ac.get(it.get("ac_id")) or {}).get("verdict")
        if bundle_by_ac.get(it.get("ac_id")) is None:
            return 0
        return {"fail": 1, "uncertain": 2, "pass": 4}.get(vd, 3)
    ordered = sorted(intents, key=_rank)

    cards = "".join(_render_ac_card(it, bundle_by_ac.get(it.get("ac_id")),
                                    judge_by_ac.get(it.get("ac_id")), base_dir) for it in ordered)

    checks_html = ""
    if gate_result and isinstance(gate_result.get("checks"), dict):
        rows = []
        for k, fails in gate_result["checks"].items():
            if fails:
                rows.append(f'<div class="c-bad">✗ {_esc(k)}</div>')
                for f in fails:
                    rows.append(f'<div class="c-bad">&nbsp;&nbsp;- {_esc(f)}</div>')
            else:
                rows.append(f'<div class="c-ok">✓ {_esc(k)}</div>')
        checks_html = '<div class="lbl">结构检查（六道）</div><div class="checks">' + "".join(rows) + "</div>"

    derived = ""
    if gate_result and gate_result.get("summary", {}).get("required_surfaces_derived"):
        derived = "，".join(gate_result["summary"]["required_surfaces_derived"])

    feature = _esc(plan.get("feature") or "验证报告")
    return (
        "<!DOCTYPE html>\n<html lang=\"zh\"><head><meta charset=\"utf-8\"/>"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"/>"
        f"<title>验证报告 · {feature}</title><style>{_CSS}</style></head><body><div class=\"wrap\">"
        f"<h1>fastship 验证报告 · {feature}</h1>"
        "<div class=\"sub\">AC 驱动旅程验证 —— 证据/截图/裁判，对照原始证据判断诚实性</div>"
        f"<div class=\"banner {decision}\">{_esc(banner_txt)}</div>"
        f"<div class=\"meta\"><span>AC 共 <b>{len(intents)}</b></span>"
        f"<span>pass <b class=\"ok\">{counts['pass']}</b></span>"
        f"<span>fail <b class=\"no\">{counts['fail']}</b></span>"
        f"<span>uncertain <b>{counts['uncertain']}</b></span>"
        + (f"<span>diff 必需表面 <b>{_esc(derived)}</b></span>" if derived else "")
        + "</div>"
        + checks_html
        + cards
        + "</div></body></html>"
    )


def render_verify_file(plan_path, evidence_dir, judge_path, gate_result_path=None, out_path=None):
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    with open(judge_path, encoding="utf-8") as f:
        judge = json.load(f)
    bundles = _load_bundles(evidence_dir)
    gate_result = None
    if gate_result_path and os.path.exists(gate_result_path):
        try:
            with open(gate_result_path, encoding="utf-8") as f:
                gate_result = json.load(f)
        except Exception:
            gate_result = None
    html = render_verify_html(plan, bundles, judge, gate_result, base_dir=evidence_dir)
    if not out_path:
        out_path = os.path.join(evidence_dir, "verify-report.verify-report.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def open_in_browser(path):
    """Best-effort 打开。env FASTSHIP_VERIFY_HTML_OPEN: never/always/auto(默认，headless 跳过)。"""
    mode = (os.environ.get("FASTSHIP_VERIFY_HTML_OPEN") or "auto").strip().lower()
    if mode in ("0", "no", "false", "never", "off"):
        return False
    if mode in ("", "auto"):
        if os.environ.get("CI") or os.environ.get("FASTSHIP_HEADLESS"):
            return False
    try:
        import subprocess
        if sys.platform == "darwin":
            subprocess.Popen(["open", path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        elif sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]  # noqa
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render fastship AC 验证报告为自包含 HTML")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--evidence-dir", required=True)
    ap.add_argument("--judge", required=True)
    ap.add_argument("--gate-result", default=None)
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)
    out = render_verify_file(args.plan, args.evidence_dir, args.judge, args.gate_result, args.out)
    print(out)
    if not args.no_open:
        open_in_browser(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
