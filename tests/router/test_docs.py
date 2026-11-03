import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FIX, SCRIPTS

SKILL = SCRIPTS.parent
CLI = SCRIPTS / "router.py"
CMD = re.compile(r"(?:router\.py|\$ROUTER_CLI)(?:\s+--root\s+\S+)?\s+([a-z][a-z-]+)")


def help_commands():
    out = subprocess.run([sys.executable, str(CLI), "--help"], capture_output=True, text=True).stdout
    return set(re.search(r"\{([a-z,-]+)\}", out).group(1).split(","))


def test_docs_reference_only_existing_subcommands():
    known = help_commands()
    used = set()
    for doc in ("SKILL.md", "worker.md", "templates/brief.md", "templates/triage.md"):
        used |= set(CMD.findall((SKILL / doc).read_text(encoding="utf-8")))
    assert used, "文档里一个 router.py 子命令都没引用"
    assert used - known == set()
    # 协议依赖的关键子命令都要在文档里出现过
    assert {"discover", "tick", "watch", "draft", "start", "answer", "submit-card", "decide", "tell",
            "emit", "progress", "inbox", "status"} <= used


def test_worker_doc_lists_exactly_three_stop_conditions():
    text = (SKILL / "worker.md").read_text(encoding="utf-8")
    sec = re.search(r"^##[^\n]*三种情况[^\n]*\n(.*?)(?=^## )", text, re.S | re.M)
    assert sec, "worker.md 缺「只在三种情况停」一节"
    items = re.findall(r"^\d+\.\s", sec.group(1), re.M)
    assert len(items) == 3


def test_worker_doc_has_eight_item_card_template():
    text = (SKILL / "worker.md").read_text(encoding="utf-8")
    sec = re.search(r"^##[^\n]*方向卡模板[^\n]*\n(.*?)(?=^## )", text, re.S | re.M).group(1)
    assert [int(n) for n in re.findall(r"^(\d+)\.\s", sec, re.M)][:8] == list(range(1, 9))
    assert "$ROUTER_HOME/cards/" in text and "progress card_ready" in text


def test_skill_frontmatter_and_triggers():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
    assert re.search(r"^name: router$", fm, re.M)
    for trig in ("router", "派发周报", "看板", "start"):
        assert trig in fm


def test_triage_schema_matches_draft_loader(tmp_path):
    text = (SKILL / "templates" / "triage.md").read_text(encoding="utf-8")
    example = json.loads(re.search(r"```json\n(.*?)\n```", text, re.S).group(1))
    (tmp_path / "t.json").write_text(json.dumps(example, ensure_ascii=False))
    home = tmp_path / ".claude" / "router"; home.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({"repo": "o/r", "lark_config": str(tmp_path / "none.json")}))
    (home / "surfaces.json").write_text((FIX / "surfaces.json").read_text())
    r = subprocess.run([sys.executable, str(CLI), "--root", str(tmp_path), "draft", "--from", str(tmp_path / "t.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    keys = {"title", "weight", "priority", "why", "problem", "problem_anchor", "clue", "surfaces", "tags",
            "depends_on", "covered_by", "decision", "reason"}
    assert all(set(t) == keys for t in example["tickets"])


def test_direction_review_output_schema():
    text = (SKILL / "templates" / "direction_review.md").read_text(encoding="utf-8")
    assert '"pass"' in text and '"challenges"' in text
    for q in ("派单时的那个问题", "修症状", "更简单"):
        assert q in text


# ---- R7：方向审的 codex 只走 review-pack（⑨） ------------------------------------------

def _section(text, head):
    m = re.search(r"^## " + re.escape(head) + r"[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert m, f"缺「## {head}」一节"
    return m.group(1)


def test_skill_codex_review_goes_through_review_pack():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    sec = _section(text, "4.")
    assert "review-pack" in sec and "review-canary" in sec
    assert "canary_stale" in sec and "canary_leak" in sec                  # 过期先补跑金丝雀；泄漏要告诉用户
    assert 'codex exec -s read-only "<同一份材料>"' not in text            # 手拼命令删掉


# ---- R8 + H3：分诊模板认识周报契约，且有行为级检查（⑩） ---------------------------------

EXCLUSION_CODES = ("instrument", "instrument_missing", "before_has", "after_has", "crossing",
                   "window_short", "evidence_erased", "immature")
TRIAGE_EVAL = FIX / "triage_eval"
CHECK = Path(__file__).resolve().parent / "triage_eval_check.py"


def _rules(text):
    sec = _section(text, "规则")
    return {int(n): body for n, body in re.findall(r"^(\d+)\.\s(.*?)(?=^\d+\.\s|\Z)", sec, re.S | re.M)}


def test_triage_knows_weekly_progress_contract():
    text = (SKILL / "templates" / "triage.md").read_text(encoding="utf-8")
    for term in ("why_text", "no_release", "_unconfirmed", "baseline.instrument", "prior_same_instrument",
                 *EXCLUSION_CODES):
        assert term in text, term
    rules = _rules(text)
    r8 = rules[8]
    assert "复查" in r8 and "flat" in r8 and "why == []" in r8
    for code in (*EXCLUSION_CODES, "no_release", "small"):
        assert code in r8, f"第 8 条没列出排除项 {code}"
    assert "_unconfirmed" in rules[1] and "不要自己拿 z 推判定" in rules[1]


def _fixture():
    week = next((TRIAGE_EVAL / "runs").iterdir())
    summary = json.loads((week / "summary.json").read_text(encoding="utf-8"))
    releases = json.loads((TRIAGE_EVAL / "state" / "releases.json").read_text(encoding="utf-8"))
    return week.name, summary, releases


def _released_old(week, releases, days=14):
    from datetime import date
    w = date.fromisoformat(week)
    return {m for r in releases if (w - date.fromisoformat(r["date"])).days >= days for m in r["metrics"]}


def _code(w):
    return w.split(":", 1)[0]


def _strings(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)
    elif isinstance(o, str):
        yield o


def test_triage_eval_fixtures_are_synthetic():
    import github
    assert TRIAGE_EVAL.is_dir(), "缺 tests/router/fixtures/triage_eval/"
    files = [p for p in TRIAGE_EVAL.rglob("*") if p.is_file()]
    assert len(files) >= 3
    _, summary, _ = _fixture()
    assert {"m_hz", "m_flat_dirty", "m_flat_clean"} <= set(summary["progress"])
    for f in files:
        if f.suffix != ".json":
            continue
        for s in _strings(json.loads(f.read_text(encoding="utf-8"))):
            assert not github._is_uid(s), (f.name, s)                           # 不许出现像 uid 的值
            long_en = re.search(r"[A-Za-z]+(?: [A-Za-z]+){3,}", s)
            long_zh = len(re.findall(r"[一-鿿]", s)) >= 12
            assert not (len(s) >= 20 and (long_en or long_zh)), (f.name, s)     # 不许有像用户原话的长句


def test_triage_eval_fixture_covers_every_exclusion_code():               # H3：逐类覆盖
    week, summary, releases = _fixture()
    prog = summary["progress"]
    names = list(prog)
    assert not [(a, b) for a in names for b in names if a != b and a in b], "指标名互为子串，检查器会认错"
    old = _released_old(week, releases)
    for code in EXCLUSION_CODES:                                             # 每个排除码各有一个「只因它被排除」的已上线 flat
        hit = [m for m, p in prog.items() if m in old and p["v"] == "flat" and [_code(w) for w in p["why"]] == [code]
               and p.get("why_text")]
        assert hit, f"没有只带 {code} 的已上线 flat 指标"
        if code in ("before_has", "after_has", "crossing"):
            assert ":" in prog[hit[0]]["why"][0]
    assert [m for m, p in prog.items() if m in old and p["v"] == "flat" and len(p["why"]) >= 2]      # m_flat_dirty 一类
    assert [m for m, p in prog.items() if p["v"] == "no_release" and abs(p.get("z", 0)) >= 2]        # 有趋势、无上线可归因
    assert [m for m, p in prog.items() if m in old and p["v"] == "small" and p["why"] and p.get("why_text")]
    assert [m for m, p in prog.items() if p["v"].endswith("_unconfirmed") and abs(p["z"]) >= 2]     # 被封顶的「显著」
    assert [m for m, p in prog.items() if p["v"] == "worse" and p["why"] == []]                     # 正对照：该出票
    clean = [m for m in old if m in prog and prog[m]["v"] == "flat" and prog[m]["why"] == []]
    assert summary["instrument"] != summary["baseline"]["instrument"]                                # 跨口径
    assert summary["prior_same_instrument"]["instrument"] == summary["instrument"]
    prior, base = summary["prior_same_instrument"]["metrics"], summary["baseline"]["metrics"]
    with_prior, without = [m for m in clean if m in prior], [m for m in clean if m not in prior]
    assert with_prior and without, "要同时有「有同口径基线」和「无同口径基线」两种干净 flat"
    for m in clean:
        assert "value" in prog[m] and m in base
        vals = [str(prog[m]["value"]), str(base[m]["value"])] + ([str(prior[m]["value"])] if m in prior else [])
        assert not [(a, b) for a in vals for b in vals if a != b and a in b], f"{m} 的数值互为子串"


# ---- 检查器：好输出 exit 0；每一类坏输出 exit 1 并点名 ------------------------------------

def _ticket(title, metric, decision="dispatch", users="", reason="r"):
    return {"title": title, "weight": "light", "priority": 5,
            "why": {"metric": metric, "users": users, "evidence": "runs/2099-01-05/summary.json#progress"},
            "problem": "p", "problem_anchor": "", "clue": "", "surfaces": [], "tags": [], "depends_on": [],
            "covered_by": "", "decision": decision, "reason": reason}


def _good():
    week, summary, releases = _fixture()
    prog, old = summary["progress"], _released_old(week, releases)
    prior = summary["prior_same_instrument"]["metrics"]
    date_of = {m: r["date"] for r in releases for m in r["metrics"]}
    tickets = [_ticket(f"{m} 确认变差", m) for m, p in prog.items() if p["v"] == "worse"]
    clean = sorted(m for m in old if m in prog and prog[m]["v"] == "flat" and prog[m]["why"] == [])
    for m in clean:
        before = str(prior[m]["value"]) if m in prior else "无同口径基线"
        tickets.append(_ticket(f"方向可能错：{m}", m, users=f"上线 {date_of[m]}；前 {before} → 后 {prog[m]['value']}"))
    not_reviewed = [{"metric": m, "reason": f"不出复查票：{prog[m].get('why_text', '')}"}
                    for m in sorted(old) if m in prog and m not in clean]
    return {"batch": week, "tickets": tickets, "not_reviewed": not_reviewed}


def _check(tmp_path, out):
    f = tmp_path / "triage.json"; f.write_text(json.dumps(out, ensure_ascii=False))
    r = subprocess.run([sys.executable, str(CHECK), str(f)], capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout + r.stderr


def _flagged(text, cls, metric):
    return any(cls in line and metric in line for line in text.splitlines())


def test_triage_eval_checker_discriminates(tmp_path):
    rc, text = _check(tmp_path, _good())
    assert rc == 0, text
    bad = _good(); bad["tickets"].append(_ticket("m_hz 显著上升", "m_hz"))                       # 被封顶的判定出了票
    rc, text = _check(tmp_path, bad)
    assert rc == 1 and _flagged(text, "unconfirmed_dispatch", "m_hz"), text
    bad = _good(); bad["tickets"].append(_ticket("方向可能错：m_flat_dirty", "m_flat_dirty"))  # 口径不干净的 flat 出了复查票
    rc, text = _check(tmp_path, bad)
    assert rc == 1 and _flagged(text, "review_not_clean", "m_flat_dirty"), text
    bad = _good(); bad["tickets"] = [t for t in bad["tickets"] if t["why"]["metric"] != "m_flat_clean"]   # 漏了该出的复查票
    rc, text = _check(tmp_path, bad)
    assert rc == 1 and _flagged(text, "missing_review", "m_flat_clean"), text


def _excluded_by(code):
    week, summary, releases = _fixture()
    old = _released_old(week, releases)
    return next(m for m, p in summary["progress"].items()
                if m in old and p["v"] == "flat" and [_code(w) for w in p["why"]] == [code])


@pytest.mark.parametrize("code", EXCLUSION_CODES)
def test_triage_eval_checker_flags_review_for_each_exclusion_code(tmp_path, code):       # H3
    m = _excluded_by(code)
    bad = _good(); bad["tickets"].append(_ticket(f"方向可能错：{m}", m))
    rc, text = _check(tmp_path, bad)
    assert rc == 1 and _flagged(text, "review_not_clean", m), text


def test_triage_eval_checker_flags_no_release_small_why_text_and_baseline(tmp_path):    # H3
    week, summary, releases = _fixture()
    prog, old = summary["progress"], _released_old(week, releases)
    no_rel = next(m for m, p in prog.items() if p["v"] == "no_release")
    small = next(m for m, p in prog.items() if p["v"] == "small" and m in old)
    worse = next(m for m, p in prog.items() if p["v"] == "worse")
    prior, base = summary["prior_same_instrument"]["metrics"], summary["baseline"]["metrics"]
    clean = [m for m in old if m in prog and prog[m]["v"] == "flat" and prog[m]["why"] == []]
    with_prior = next(m for m in clean if m in prior)
    without = next(m for m in clean if m not in prior)
    cases = []
    bad = _good(); bad["tickets"].append(_ticket(f"{no_rel} 趋势", no_rel)); cases.append((bad, "unconfirmed_dispatch", no_rel))
    bad = _good(); bad["tickets"].append(_ticket(f"{small} 变化", small)); cases.append((bad, "unconfirmed_dispatch", small))
    bad = _good(); bad["tickets"].append(_ticket(f"方向可能错：{small}", small)); cases.append((bad, "review_not_clean", small))
    bad = _good(); bad["tickets"] = [t for t in bad["tickets"] if t["why"]["metric"] != worse]
    cases.append((bad, "missing_dispatch", worse))                                       # 正对照：确认变差必须出票
    bad = _good(); bad["not_reviewed"] = [x for x in bad["not_reviewed"] if x["metric"] != small]
    cases.append((bad, "why_text", small))                                               # 没交代为什么不复查
    bad = _good()
    for x in bad["not_reviewed"]:
        if x["metric"] == small:
            x["reason"] = "证据不足"                                                       # 换了说法、没贴 why_text 原文
    cases.append((bad, "why_text", small))
    bad = _good()
    for t in bad["tickets"]:
        if t["why"]["metric"] == with_prior:                                             # 跨口径还拿 baseline 当「前」
            t["why"]["users"] = f"前 {base[with_prior]['value']} → 后 {prog[with_prior]['value']}"
    cases.append((bad, "baseline", with_prior))
    bad = _good()
    for t in bad["tickets"]:
        if t["why"]["metric"] == without:                                                # 没有同口径基线却编了一个
            t["why"]["users"] = f"前 {base[without]['value']} → 后 {prog[without]['value']}"
    cases.append((bad, "baseline", without))
    bad = _good(); bad["tickets"].append(_ticket(f"方向可能错：{with_prior}（重复）", with_prior))
    cases.append((bad, "missing_review", with_prior))                                    # 恰好一张
    for out, cls, metric in cases:
        rc, text = _check(tmp_path, out)
        assert rc == 1 and _flagged(text, cls, metric), (cls, metric, text)


def test_triage_eval_template_documents_not_reviewed_and_baseline_rule():             # H3：检查器要的输出，模板得写明
    text = (SKILL / "templates" / "triage.md").read_text(encoding="utf-8")
    assert "not_reviewed" in text and "无同口径基线" in text
    rule = [b for b in _rules(text).values() if "prior_same_instrument" in b]
    assert rule and "baseline.instrument" in rule[0]
