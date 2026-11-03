#!/usr/bin/env python3
"""分诊行为级检查器：拿 fixtures/triage_eval/ 这份合成周报，判一份分诊输出（triage.json）照没照 templates/triage.md 做。

  python3 tests/router/triage_eval_check.py <分诊输出 json> [--fixture <triage_eval 目录>]

退出码 0 = 过；1 = 有违反（每条一行「<类>: <指标> — <说明>」）或输出读不了。类：
  unconfirmed_dispatch  v 不是 worse 却出了派发票（better 不出票；*_unconfirmed、no_release、small、flat 都不是确认过的变差）
  review_not_clean      复查票（标题「方向可能错：」）给了不是干净 flat 的指标
  missing_review        上线满 2 周、v == flat 且 why == [] 的指标，复查票不是恰好 1 张
  missing_dispatch      v == worse 的指标没有派发票
  why_text              上线满 2 周、没出复查票的指标，not_reviewed 里没有它，或 reason 没逐字带 why_text
  baseline              复查票的「前值」用了跨口径的 baseline，或没用同口径前值 / 没写「无同口径基线」
  schema / batch        输出结构不对 / 批次不是这一周
既用于 tests/router/test_docs.py 的判别力测试，也用于真 Opus 分诊叶子的行为级验收（plan v4 A8）。
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "triage_eval"
REVIEW = "方向可能错："
CONFIRMED = {"better", "worse"}      # 确认过的变化：第 8 条不把它们当复查 / not_reviewed 候选（按第 1 条处理）
DISPATCH = {"worse"}                 # 第 1 条：只有 worse 出票查原因；better 不出票
MATURE_DAYS = 14                     # triage.md 第 8 条：上线已满 2 周
NO_BASE = "无同口径基线"


def load_fixture(root: Path):
    weeks = sorted(d for d in (root / "runs").iterdir() if d.is_dir())
    if len(weeks) != 1:
        raise SystemExit(f"夹具 runs/ 下应恰好一周，实际 {[w.name for w in weeks]}")
    summary = json.loads((weeks[0] / "summary.json").read_text(encoding="utf-8"))
    releases = json.loads((root / "state" / "releases.json").read_text(encoding="utf-8"))
    return weeks[0].name, summary, releases


def check(out, week, summary, releases) -> list:
    """→ [(类, 指标, 说明)]；空 = 过。"""
    if not isinstance(out, dict) or not isinstance(out.get("tickets"), list):
        return [("schema", "-", "顶层要有 tickets 列表")]
    probs = []
    if out.get("batch") != week:
        probs.append(("batch", "-", f"batch 应为 {week}，实际 {out.get('batch')!r}"))
    prog = summary["progress"]
    names = list(prog)
    w = date.fromisoformat(week)
    mature = {m for r in releases if (w - date.fromisoformat(r["date"])).days >= MATURE_DAYS for m in r["metrics"]}
    tickets = [t for t in out["tickets"] if isinstance(t, dict)]

    def metrics_of(t):
        why = t.get("why") if isinstance(t.get("why"), dict) else {}
        hit = [m for m in names if m in str(why.get("metric", ""))]
        return hit or [m for m in names if m in str(t.get("title", ""))]

    review = [t for t in tickets if str(t.get("title", "")).startswith(REVIEW)]
    dispatch = [t for t in tickets if t not in review and t.get("decision", "dispatch") == "dispatch"]

    for t in dispatch:                                                        # 第 1 条
        for m in metrics_of(t):
            if prog[m]["v"] not in DISPATCH:
                why = "better 不出票" if prog[m]["v"] == "better" else "不是确认过的变差"
                probs.append(("unconfirmed_dispatch", m, f"v={prog[m]['v']}（{why}），却出了派发票「{t.get('title')}」"))
    for m in names:
        if prog[m]["v"] == "worse" and not any(m in metrics_of(t) for t in dispatch):
            probs.append(("missing_dispatch", m, "确认变差，却没有派发票"))

    clean = [m for m in names if m in mature and prog[m]["v"] == "flat" and prog[m]["why"] == []]
    count = {m: 0 for m in clean}
    for t in review:                                                          # 第 8 条
        for m in metrics_of(t):
            if m in count:
                count[m] += 1
            else:
                probs.append(("review_not_clean", m, f"v={prog[m]['v']}、why={prog[m]['why']}：不能当复查依据"))
    for m, n in count.items():
        if n != 1:
            probs.append(("missing_review", m, f"干净的 flat 应恰好 1 张复查票，实际 {n} 张"))

    candidates = [m for m in names if m in mature and m not in clean and prog[m]["v"] not in CONFIRMED]
    nr = [x for x in out.get("not_reviewed") or [] if isinstance(x, dict)]
    for m in candidates:
        wt = prog[m].get("why_text") or ""
        mine = [x for x in nr if m in str(x.get("metric", ""))]
        if not mine:
            probs.append(("why_text", m, "上线满 2 周、没出复查票，not_reviewed 里却没有它"))
        elif wt and not any(wt in str(x.get("reason", "")) for x in mine):
            probs.append(("why_text", m, f"not_reviewed 的 reason 没逐字带周报说明「{wt}」"))

    inst, base = summary.get("instrument"), summary.get("baseline") or {}
    prior = summary.get("prior_same_instrument") or {}
    same = inst is not None and base.get("instrument") == inst
    prior_m = (prior.get("metrics") or {}) if prior.get("instrument") == inst else {}
    base_m = base.get("metrics") or {}
    for t in review:
        why = t.get("why") if isinstance(t.get("why"), dict) else {}
        text = " ".join(str(v) for v in why.values())
        for m in metrics_of(t):
            if m not in count:
                continue
            bv = str(base_m[m]["value"]) if m in base_m else None
            if same:
                if bv and bv not in text:
                    probs.append(("baseline", m, f"同口径：前值应为 baseline 的 {bv}"))
                continue
            if bv and bv in text:
                probs.append(("baseline", m, f"跨口径（{base.get('instrument')} ≠ {inst}）却写了 baseline 的 {bv}"))
            if m in prior_m:
                pv = str(prior_m[m]["value"])
                if pv not in text:
                    probs.append(("baseline", m, f"前值应取 prior_same_instrument 的 {pv}"))
            elif NO_BASE not in text:
                probs.append(("baseline", m, f"没有同口径前值，why 里应写「{NO_BASE}」"))
    return probs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("output")
    ap.add_argument("--fixture", default=str(FIXTURE))
    a = ap.parse_args(argv)
    week, summary, releases = load_fixture(Path(a.fixture))
    try:
        out = json.loads(Path(a.output).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"schema: - — 读不了分诊输出：{e}")
        return 1
    probs = check(out, week, summary, releases)
    for cls, m, msg in probs:
        print(f"{cls}: {m} — {msg}")
    if probs:
        print(f"FAIL：{len(probs)} 处违反（夹具 {week}）")
        return 1
    print(f"OK：分诊输出符合 triage.md（夹具 {week}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
