#!/usr/bin/env python3
"""
verify_html.py — 把 AC 驱动【用户旅程】验证渲染成离线自包含 HTML 测试报告。

spec §7。永远生成、不进可信账本（gitignore）、生成失败不阻断。
镜像 scripts/plan_html.py 的自包含单文件套路：截图 base64 内嵌，无外链。
大图自动降采样重压（Pillow 在则缩到 ~1600px/JPEG，多 MB → 几百 KB，必内嵌且报告不爆）；
Pillow 不可用时原图直嵌（≤ 内嵌上限），只有超大/缺失/损坏才退化并写明原因。

报告 = 头号交付：把【正常用户路径】当成时间线讲给人看——
  - 顶部总判（PASS / FAIL / 待确认）+ 旅程/步数 + AC 通过数 + diff 派生必经表面
  - 结构检查（六道）逐条 ✓/✗
  - 每条 journey 一条【时间线】：有序步骤，每步一张截图 + 人读说明 + 该步证了哪条 AC
    （差分旅程的 ON/OFF 步在时间线里带状态标）
  - AC 裁判小结：逐条 verdict + reason + 引用的截图

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
--ok:#3fb950;--bad:#f85149;--warn:#d29922;--accent:#58a6ff;--on:#58a6ff;--off:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:26px 0 10px;border-bottom:1px solid var(--ln);padding-bottom:6px}
.sub{color:var(--mut);margin-bottom:20px}
.banner{padding:14px 18px;border-radius:10px;font-size:18px;font-weight:600;margin-bottom:8px}
.banner.pass{background:rgba(63,185,80,.14);border:1px solid var(--ok);color:var(--ok)}
.banner.fail{background:rgba(248,81,73,.14);border:1px solid var(--bad);color:var(--bad)}
.banner.surface{background:rgba(210,153,34,.14);border:1px solid var(--warn);color:var(--warn)}
.meta{display:flex;gap:18px;flex-wrap:wrap;color:var(--mut);font-size:13px;margin-bottom:8px}
.meta b{color:var(--tx)}
.checks{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;margin:8px 0 4px}
.checks .c-ok{color:var(--ok)}.checks .c-bad{color:var(--bad)}
.jtitle{font-size:15px;font-weight:600;margin:18px 0 2px}
.jtitle .jid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--accent);margin-right:8px}
.tl{list-style:none;margin:8px 0 0;padding:0;border-left:2px solid var(--ln)}
.tl li{position:relative;padding:6px 0 18px 22px}
.tl li::before{content:"";position:absolute;left:-7px;top:9px;width:12px;height:12px;border-radius:50%;background:var(--accent);border:2px solid var(--bg)}
.tl li.no::before{background:var(--bad)}
.tl li.on::before{background:var(--on)}
.tl li.off::before{background:var(--off)}
.sline{font-size:13.5px}
.snum{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--mut);margin-right:6px}
.sf{display:inline-block;font-size:11px;padding:1px 7px;border-radius:5px;background:#12151b;border:1px solid var(--ln);color:var(--accent);margin-right:6px}
.stag{font-size:11px;padding:1px 7px;border-radius:5px;margin-right:6px;font-weight:600}
.stag.on{background:rgba(88,166,255,.16);color:var(--on)}
.stag.off{background:rgba(210,153,34,.16);color:var(--off)}
.act{font-weight:600}
.cap{color:var(--tx)}
.proves{color:var(--mut);font-size:12px;margin:2px 0}
.proves b{color:var(--ok)}
.shot{display:inline-block;border:1px solid var(--ln);border-radius:8px;overflow:hidden;max-width:420px;margin-top:6px}
.shot .cn{font-size:11px;color:var(--mut);padding:4px 8px;background:#12151b}
.shot img{display:block;max-width:420px;height:auto}
.facts{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--mut);margin-top:4px}
.facts div{padding:1px 0}
.noshot{color:var(--warn);font-size:12px;margin-top:4px}
.vcard{background:var(--card);border:1px solid var(--ln);border-radius:10px;padding:12px 16px;margin-bottom:10px}
.vcard.fail{border-color:var(--bad)}
.vcard.uncertain{border-color:var(--warn)}
.acid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--accent)}
.vbadge{font-weight:700;padding:2px 8px;border-radius:5px;font-size:12px;margin-right:8px}
.vbadge.pass{background:rgba(63,185,80,.16);color:var(--ok)}
.vbadge.fail{background:rgba(248,81,73,.16);color:var(--bad)}
.vbadge.uncertain{background:rgba(210,153,34,.16);color:var(--warn)}
.vbadge.missing{background:rgba(248,81,73,.16);color:var(--bad)}
.refs{margin:6px 0 0;padding-left:18px;color:var(--mut);font-size:12.5px}
.ok{color:var(--ok)}.no{color:var(--bad)}
"""

# Pillow 不可用时的原图直嵌上限。Pillow 在则先降采样，几乎不会触顶；抬到 16MB 让真实全页 PNG 也能内嵌。
MAX_EMBED_BYTES = 16 * 1024 * 1024
_DOWNSCALE_TRIGGER = 512 * 1024     # 超过此体积才尝试降采样；小图原样内嵌（保真省 CPU）
_DOWNSCALE_MAX_W = 1600             # 报告 CSS 仅显示到 420px，1600 宽已足够清晰且体积小
_JPEG_QUALITY = 85
_STATE_ORDER = {"default": 0, "on": 1, "off": 2}


def _esc(s):
    return _html.escape(str(s if s is not None else ""))


def _downscale_data_uri(rp):
    """用 Pillow 把大图缩到 _DOWNSCALE_MAX_W 宽并重压，返回 data URI。
    Pillow 不可用 / 打不开 / 编码失败 → None（调用方回退原图直嵌）。"""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        import io
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS
        with Image.open(rp) as im:
            im.load()
            has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
            if im.width > _DOWNSCALE_MAX_W:
                h = max(1, round(im.height * _DOWNSCALE_MAX_W / im.width))
                im = im.resize((_DOWNSCALE_MAX_W, h), resample)
            buf = io.BytesIO()
            if has_alpha:
                im.convert("RGBA").save(buf, format="PNG", optimize=True)
                mime = "png"
            else:
                im.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
                mime = "jpeg"
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/{mime};base64,{b64}"
    except Exception:
        return None


def _img_data_uri(path, base_dir):
    """返回 (data_uri | None, reason | None)。内嵌成功是默认路径；失败时 reason 写明原因便于排查。

    路线 A：大图 → Pillow 降采样重压（多 MB → 几百 KB，必内嵌、报告不爆）。
    路线 B：小图 / 降采样失败 / Pillow 不可用 → 原图直嵌（≤ MAX_EMBED_BYTES）。
    路线 C：超大且无法降采样 → 退化，给原因。
    """
    rp = path if os.path.isabs(path) else os.path.join(base_dir, path)
    try:
        if not os.path.exists(rp):
            return None, "文件缺失"
        size = os.path.getsize(rp)
    except OSError as e:
        return None, f"读取失败（{e.__class__.__name__}）"
    if size == 0:
        return None, "文件为空（0 字节）"

    # 路线 A：大图优先降采样（几乎总能内嵌且体积小）
    if size > _DOWNSCALE_TRIGGER:
        uri = _downscale_data_uri(rp)
        if uri:
            return uri, None

    # 路线 B：原图直嵌（小图，或降采样不可用/失败时的兜底）
    if size <= MAX_EMBED_BYTES:
        ext = os.path.splitext(rp)[1].lower().lstrip(".") or "png"
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif"}.get(ext, "png")
        try:
            with open(rp, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return f"data:image/{mime};base64,{b64}", None
        except Exception as e:
            return None, f"读取失败（{e.__class__.__name__}）"

    # 路线 C：超大且无法降采样
    return None, (f"图 {size / 1024 / 1024:.1f}MB 超内嵌上限 "
                  f"{MAX_EMBED_BYTES // 1024 // 1024}MB（装 Pillow 可自动降采样内嵌）")


def _load_journeys(evidence_dir):
    """读所有 *.journey.json，保持文件名排序（旅程间确定顺序）。"""
    out = []
    if not os.path.isdir(evidence_dir):
        return out
    for fn in sorted(os.listdir(evidence_dir)):
        if fn.endswith(".journey.json"):
            try:
                with open(os.path.join(evidence_dir, fn), encoding="utf-8") as f:
                    out.append(json.load(f))
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


def _render_step(step, base_dir):
    state = step.get("state", "default") or "default"
    ok = step.get("ok", True)
    li_cls = []
    if not ok:
        li_cls.append("no")
    if state in ("on", "off"):
        li_cls.append(state)
    out = [f'<li class="{" ".join(li_cls)}">']
    tag = f'<span class="stag {state}">{_esc(state.upper())}</span>' if state in ("on", "off") else ""
    caption = step.get("caption") or step.get("intent") or ""
    mark = '<span class="ok">✓</span>' if ok else '<span class="no">✗</span>'
    out.append(
        f'<div class="sline">{mark} <span class="snum">#{_esc(step.get("no", "?"))}</span>'
        f'<span class="sf">{_esc(step.get("surface", "?"))}</span>{tag}'
        f'<span class="act">{_esc(step.get("action", ""))}</span> — <span class="cap">{_esc(caption)}</span></div>'
    )
    proves = step.get("proves_acs") or []
    if proves:
        out.append('<div class="proves">证 AC：' + "，".join(f"<b>{_esc(a)}</b>" for a in proves) + "</div>")
    shot = step.get("screenshot")
    if isinstance(shot, str) and shot.strip():
        uri, reason = _img_data_uri(shot, base_dir)
        cn = _esc(os.path.basename(shot))
        if uri:
            out.append(f'<div class="shot"><div class="cn">{cn}</div><img src="{uri}"/></div>')
        else:
            out.append(f'<div class="noshot">截图未内嵌（{_esc(reason)}）：{_esc(shot)}</div>')
    facts = []
    for n in (step.get("network") or []):
        facts.append(f"<div>↳ {_esc(n.get('method', '?'))} {_esc(n.get('url', '?'))} → "
                     f"{_esc(n.get('status', '?'))} @{_esc(n.get('target', '?'))}</div>")
    for dfact in (step.get("dom_facts") or []):
        present = "✓在" if dfact.get("present") else "✗无"
        facts.append(f"<div>↳ DOM {_esc(dfact.get('selector_intent', '?'))}: {present}</div>")
    if facts:
        out.append('<div class="facts">' + "".join(facts) + "</div>")
    out.append("</li>")
    return "".join(out)


def _render_journey(journey, plan_titles, base_dir):
    jid = journey.get("journey_id", "?")
    title = journey.get("title") or plan_titles.get(jid, "")
    steps = [s for s in (journey.get("steps") or []) if isinstance(s, dict)]
    # 时间线按 no 升序；同 no 时差分 default<on<off 稳定
    steps = sorted(steps, key=lambda s: (s.get("no", 0), _STATE_ORDER.get(s.get("state", "default"), 9)))
    out = [f'<div class="jtitle"><span class="jid">{_esc(jid)}</span>{_esc(title)}</div>']
    out.append('<ul class="tl">')
    for st in steps:
        out.append(_render_step(st, base_dir))
    out.append("</ul>")
    return "".join(out)


def _render_verdict(v):
    vd = v.get("verdict") or "?"
    cls = "fail" if vd == "fail" else ("uncertain" if vd == "uncertain" else "")
    out = [f'<div class="vcard {cls}">',
           f'<span class="vbadge {_esc(vd)}">{_esc(vd).upper()}</span>'
           f'<span class="acid">{_esc(v.get("ac_id"))}</span> &nbsp;{_esc(v.get("reason"))}']
    refs = v.get("evidence_refs") or []
    if refs:
        out.append('<ul class="refs">')
        for r in refs:
            if isinstance(r, dict):
                out.append(f'<li>{_esc(os.path.basename(str(r.get("artifact"))))} — {_esc(r.get("fact"))}</li>')
        out.append("</ul>")
    out.append("</div>")
    return "".join(out)


def render_verify_html(plan, journeys, judge, gate_result=None, base_dir="."):
    decision = _display_decision(gate_result, judge)
    banner_txt = {"pass": "✅ GATE PASS — 全 AC 通过，旅程走通",
                  "fail": "❌ GATE FAIL — 有 AC 未通过或结构检查不过",
                  "surface": "🟡 待确认 — 证据弱/不确定，请看截图后确认"}.get(decision, decision)

    plan_titles = {j.get("id"): j.get("title", "") for j in (plan.get("journeys") or []) if isinstance(j, dict)}
    counts = {"pass": 0, "fail": 0, "uncertain": 0}
    for v in judge.get("verdicts", []):
        if v.get("verdict") in counts:
            counts[v["verdict"]] += 1
    total_steps = sum(len(j.get("steps") or []) for j in journeys if isinstance(j, dict))

    # 结构检查
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
        checks_html = '<h2>结构检查（六道）</h2><div class="checks">' + "".join(rows) + "</div>"

    # 旅程时间线（头号内容）
    timelines = "".join(_render_journey(j, plan_titles, base_dir) for j in journeys if isinstance(j, dict)) \
        or '<div class="no">❌ 无任何旅程证据</div>'

    # 裁判小结（fail/uncertain 置顶）
    verdicts = [v for v in judge.get("verdicts", []) if isinstance(v, dict)]
    verdicts = sorted(verdicts, key=lambda v: {"fail": 0, "uncertain": 1, "pass": 3}.get(v.get("verdict"), 2))
    verdicts_html = "".join(_render_verdict(v) for v in verdicts) or '<div class="sub">（无裁判判定）</div>'

    derived = ""
    if gate_result and gate_result.get("summary", {}).get("required_surfaces_derived"):
        derived = "，".join(gate_result["summary"]["required_surfaces_derived"])
    walked = ""
    if gate_result and gate_result.get("summary", {}).get("surfaces_walked"):
        walked = "，".join(gate_result["summary"]["surfaces_walked"])

    feature = _esc(plan.get("feature") or "验证报告")
    return (
        "<!DOCTYPE html>\n<html lang=\"zh\"><head><meta charset=\"utf-8\"/>"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"/>"
        f"<title>验证报告 · {feature}</title><style>{_CSS}</style></head><body><div class=\"wrap\">"
        f"<h1>fastship 验证报告 · {feature}</h1>"
        "<div class=\"sub\">AC 驱动【用户旅程】验证 —— 按正常用户路径走一遍，每步截图，对照判断诚实性</div>"
        f"<div class=\"banner {decision}\">{_esc(banner_txt)}</div>"
        f"<div class=\"meta\"><span>旅程 <b>{len(journeys)}</b></span>"
        f"<span>步数 <b>{total_steps}</b></span>"
        f"<span>AC pass <b class=\"ok\">{counts['pass']}</b></span>"
        f"<span>fail <b class=\"no\">{counts['fail']}</b></span>"
        f"<span>uncertain <b>{counts['uncertain']}</b></span>"
        + (f"<span>diff 必经表面 <b>{_esc(derived)}</b></span>" if derived else "")
        + (f"<span>旅程走过 <b>{_esc(walked)}</b></span>" if walked else "")
        + "</div>"
        + checks_html
        + "<h2>用户旅程（测试路径 + 每步截图）</h2>"
        + timelines
        + "<h2>AC 裁判小结</h2>"
        + verdicts_html
        + "</div></body></html>"
    )


def render_verify_file(plan_path, evidence_dir, judge_path, gate_result_path=None, out_path=None):
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    with open(judge_path, encoding="utf-8") as f:
        judge = json.load(f)
    journeys = _load_journeys(evidence_dir)
    gate_result = None
    if gate_result_path and os.path.exists(gate_result_path):
        try:
            with open(gate_result_path, encoding="utf-8") as f:
                gate_result = json.load(f)
        except Exception:
            gate_result = None
    html = render_verify_html(plan, journeys, judge, gate_result, base_dir=evidence_dir)
    if not out_path:
        out_path = os.path.join(evidence_dir, "verify-report.html")
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
    ap = argparse.ArgumentParser(description="Render fastship AC 驱动用户旅程验证报告为自包含 HTML")
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
