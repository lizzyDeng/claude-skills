"""玩法复盘（play）、判定时读体验（experience / readable）、漏斗读数（funnel）、学到的事（learned.jsonl）、
看板和 status 先讲产品。夹具全是合成的：没有真实 uid、用户原话、业务数值。"""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_state
from test_analyst import _calibrate_tmp, at, biz, cli, finish, only_run, runs, weekly_dir  # noqa: F401
from test_analyst import ap  # noqa: F401  （fixture）
from test_docs import SKILL, _section
from test_tick import proj  # noqa: F401  （fixture）

import analyst
import board
import store

UID = "5eed0001"                     # 合成 uid（8 位十六进制，防泄漏闸认它）


def env_of(p):
    return SimpleNamespace(cfg=p.cfg, home=p.home, ctx=p.ctx)


def att(n, verdict, day, note="", **extra):
    a = {"n": n, "direction": f"合成方向{n}", "at": f"2026-11-{day:02d}T00:00:00Z", "rollback": "关开关 x",
         "verdict": verdict, "note": note}
    if verdict is not None:
        a["verdict_at"] = f"2026-11-{day:02d}T01:00:00Z"
    a.update(extra)
    return a


def biz_m(attempts, metric="合成指标", status="postlaunch", **extra):
    _, surf, t = biz(attempts=attempts, metric={"name": metric}, **extra)
    return (status, surf, t)


def start_play(p, st=None, **kw):
    st = st or p.state()
    rid = analyst.start(env_of(p), st, "play", **kw)
    store.save_state(p.home / "state.json", st)
    return rid


def play_out(**over):
    d = {"mode": "play", "plays_observed": [], "hypotheses": [], "refuted_review": [], "learned": [], "escalate": None}
    d.update(over)
    return d


def hypo(**over):
    h = {"key": "synthetic-play", "title": "合成玩法假设", "segment": "合成人群", "play_change": "合成：改成…玩法",
         "why_more_engagement": "合成：看到… → 做了… → 还想接着做", "metric": {"name": "合成指标", "now": "x", "direction": "up"},
         "expected_lift_points": 20, "traffic_per_day": 40, "readable_in_days": 3, "readable_how": "合成算法",
         "cheapest_test": "合成：先改配置", "needs_code": False, "ticket": "T1", "prior_refuted": [], "new_evidence": "",
         "evidence": ["合成路径"]}
    h.update(over)
    return h


def prompt_of(p, i=-1):
    sp = p.ctx.spawned[i]
    return sp["cmd"][sp["cmd"].index("-p") + 1]


def learned_rows(p):
    f = analyst.learned_path(p.home)
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


# ---- A：play 的票况 / 上下文 / prompt -----------------------------------------------------------

def test_play_context_must_review_and_streaks():
    s = make_state(T1=biz_m([att(1, "useless", 10), att(2, "wall_broken_not_retained", 11), att(3, "harmful", 12)]),
                   T2=biz_m([att(1, "useful", 9), att(2, "useless", 12, refuted={"killed": "play", "lesson": "l"})],
                            metric="合成指标乙"),
                   T3=biz_m([att(1, "pending_sample", 12), att(2, None, 13)], metric="合成指标丙"))
    ctx = analyst.play_context(s, "2026-11-11")
    assert ctx["must_review"] == ["T1 a2", "T1 a3"]                      # 早于 since 的、复盘过的、没判完的不算
    assert ctx["streaks"] == [{"metric": "合成指标", "refs": ["T1 a1", "T1 a2", "T1 a3"]}]
    ctx = analyst.play_context(s, "2026-11-13")                          # 连败里没有新的要复盘 ⇒ 不逼 escalate
    assert ctx["must_review"] == [] and ctx["streaks"] == []
    s["analyst"] = {"runs": {"play-1": {"mode": "play", "ingested": "x", "started_at": "2026-11-11T12:00:00Z"},
                             "play-2": {"mode": "play", "failed": True, "started_at": "2026-11-12T12:00:00Z"}}}
    ctx = analyst.play_context(s, "2026-10-01")                          # 起点 = 上次跑成功的 play（失败的不算）
    assert ctx["cutoff"] == "2026-11-11T12:00:00Z" and ctx["must_review"] == ["T1 a3"]


def test_play_prompt_lists_all_attempts_prior_plays_and_memory(ap):
    d = ap.home / "analyst" / "out"; d.mkdir(parents=True)
    rs = {}
    for i in range(1, 7):
        (d / f"play-{i}.json").write_text("{}")
        rs[f"play-{i}"] = {"mode": "play", "out": str(d / f"play-{i}.json"), "ingested": "x",
                           "started_at": f"2026-11-0{i}T00:00:00Z"}
    rs["play-6"]["failed"] = True
    s = make_state(T1=biz_m([att(1, "useful", 8, "合成：墙没了"), att(2, "useless", 10, "合成：墙还在"),
                             {"n": 3, "direction": "合成老票", "historical": True, "verdict": "legacy",
                              "legacy_verdict": "没用"}]),
                   T2=biz_m([att(1, "harmful", 11, "合成：护栏变坏")], metric="合成指标乙", status="done"),
                   T4=biz_m([att(1, "useless", 11)], merged_into="T1"))
    s["analyst"] = {"runs": rs}
    store.save_state(ap.home / "state.json", s)
    text = analyst.render(env_of(ap), "play", "2026-10-30", "2026-11-13", d / "x.json")
    for w in ("T1 a1「合成方向1」", "判定 useful：合成：墙没了", "T1 a2「合成方向2」", "判定 useless：合成：墙还在",
              "合成老票", "T2 a1", "合成：护栏变坏", "（已关）", "合成指标乙"):
        assert w in text, w
    assert "T4" not in text                                              # 并进别处的票：尝试已挪走
    assert str(d / "play-1.json") not in text and str(d / "play-6.json") not in text   # 最多 4 份、失败的不算
    for i in (2, 3, 4, 5):
        assert str(d / f"play-{i}.json") in text
    assert f"{ap.home}/analyst/learned.jsonl" in text and f"{ap.home}/analyst/rejected.jsonl" in text
    assert str(ap.brief) in text and "2026-10-30" in text and "2026-11-13" in text
    must = text[text.index("## 第一步"):text.index("## 第二步")]
    assert "T1 a2" in must and "T2 a1" in must and "T1 a1" not in must    # 只有上次 play 以来的否定
    for v in ("$state_summary", "$prior_plays", "$must_review", "$streaks", "$home"):
        assert v not in text, v


# ---- A：调度（weekly 跑完后起一次，每个 ISO 周一次）------------------------------------------------

def test_play_starts_after_weekly_finishes_once_per_iso_week(ap):
    ap.put(T1=biz_m([]))
    weekly_dir(ap, "2026-11-09")
    ap.run()                                                             # 起 weekly
    (wrid, _), = runs(ap, "weekly").items()
    ap.run()
    assert runs(ap, "play") == {}                                        # weekly 还在跑：不起
    finish(ap, wrid, {"mode": "weekly", "problems": []})                 # 校准没过 ⇒ weekly 失败，也算跑完
    ap.run()
    (prid, pr), = runs(ap, "play").items()
    assert ap.state()["analyst"]["play_week"] == "2026-W46" and pr["after_weekly"] == wrid
    assert "玩法复盘" in prompt_of(ap) and f"OUT={pr['out']}" in prompt_of(ap)
    assert pr["must_review"] == [] and pr["streaks"] == []
    ap.run()
    assert len(runs(ap, "play")) == 1                                    # 在跑：不起第二个
    finish(ap, prid, play_out())
    res = ap.run()
    assert res["analyst_done"] == [{"run": prid, "mode": "play", "out": pr["out"]}]
    st = ap.state()                                                      # 同一周手动重跑 weekly：不再起 play
    w2 = analyst.start(env_of(ap), st, "weekly")
    store.save_state(ap.home / "state.json", st)
    finish(ap, w2, {"mode": "weekly", "problems": []})
    ap.run(); ap.run()
    assert len(runs(ap, "play")) == 1
    ap.ctx.clock = at(20, 1, 0)                                          # 下一周的周报 ⇒ weekly ⇒ play
    weekly_dir(ap, "2026-11-16")
    ap.run()
    w3 = max(runs(ap, "weekly"), key=lambda r: int(r.split("-")[1]))
    finish(ap, w3, {"mode": "weekly", "problems": []})
    ap.run()
    assert len(runs(ap, "play")) == 2 and ap.state()["analyst"]["play_week"] == "2026-W47"


def test_play_not_started_without_brief_or_weekly(proj):
    proj.put()
    proj.run()
    assert proj.ctx.spawned == []                                        # 没配 brief：不起
    proj.cfg["analyst"] = {"brief": str(proj.tmp / "b.md")}
    (proj.tmp / "b.md").write_text("合成 brief\n")
    proj.ctx.clock = at(13, 1, 0)
    proj.run()
    assert proj.ctx.spawned == []                                        # 没跑过 weekly：不起


# ---- A：收 play 的产出 --------------------------------------------------------------------------

def test_play_output_records_review_and_learned_then_wakes_router(ap):
    ap.put(T1=biz_m([att(1, "useful", 8), att(2, "useless", 10, "合成：墙还在")]))
    rid = start_play(ap)
    assert ap.state()["analyst"]["runs"][rid]["must_review"] == ["T1 a2"]
    before = ap.state()
    finish(ap, rid, play_out(
        refuted_review=[{"ref": "T1 a2", "killed": "mechanism", "lesson": "合成：做法没做到，玩法还可能对"}],
        hypotheses=[hypo(prior_refuted=["T1 a2"])], learned=["合成事实甲", "合成事实乙", "  "]))
    res = ap.run()
    st = ap.state()
    out = st["analyst"]["runs"][rid]["out"]
    assert res["analyst_done"] == [{"run": rid, "mode": "play", "out": out}]
    a2 = st["tickets"]["T1"]["attempts"][1]
    assert a2["refuted"] == {"killed": "mechanism", "lesson": "合成：做法没做到，玩法还可能对", "run": rid,
                             "at": a2["refuted"]["at"]} and a2["refuted"]["at"]
    assert st["queue"] == [] and not st.get("proposals") and res["new_queue"] == []      # 假设不自动落
    assert st["tickets"]["T1"]["walls"] == before["tickets"]["T1"]["walls"]
    rows = learned_rows(ap)
    assert [r["text"] for r in rows] == ["合成事实甲", "合成事实乙"]
    assert all(r["run"] == rid and r["mode"] == "play" and r["at"] for r in rows)
    assert st["analyst"]["runs"][rid]["result"] == {"hypotheses": 1, "reviewed": 1, "escalate": False, "learned": 2}
    assert "analyst_done" not in ap.run()                                # 收过就不再收
    text = analyst.render(env_of(ap), "play", "2026-10-30", "2026-11-13", ap.home / "x.json")
    assert "已复盘：否定的是做法；合成：做法没做到" in text                  # 以后的 play 都带着复盘结论
    assert analyst.play_context(ap.state(), "2026-10-01")["must_review"] == []


def _case_state(case):
    if case == "streak":
        return make_state(T1=biz_m([att(1, "useless", 9), att(2, "wall_broken_not_retained", 10), att(3, "harmful", 11)]))
    if case == "dead_play":
        return make_state(T1=biz_m([att(1, "useless", 9, refuted={"killed": "play", "lesson": "合成"}),
                                    att(2, "useless", 11)]))
    return make_state(T1=biz_m([att(1, "useful", 9), att(2, "useless", 11)]))


def _case_out(case):
    good = [{"ref": "T1 a2", "killed": "mechanism", "lesson": "合成教训"}]
    return {
        "missing": play_out(),
        "streak": play_out(refuted_review=[{"ref": f"T1 a{n}", "killed": "mechanism", "lesson": "合成"}
                                           for n in (1, 2, 3)], hypotheses=[hypo()]),
        "dead_play": play_out(refuted_review=good, hypotheses=[hypo(prior_refuted=["T1 a1"])]),
        "too_many": play_out(refuted_review=good, hypotheses=[hypo(key=f"k{i}") for i in range(4)]),
        "bad_ref": play_out(refuted_review=good + [{"ref": "T9 a1", "killed": "play", "lesson": "合成"}]),
        "not_negative": play_out(refuted_review=good + [{"ref": "T1 a1", "killed": "play", "lesson": "合成"}]),
        "bad_killed": play_out(refuted_review=[{"ref": "T1 a2", "killed": "both", "lesson": "合成"}]),
        "missing_keys": play_out(refuted_review=good, hypotheses=[{"key": "k", "title": "t"}]),
        "bad_ticket": play_out(refuted_review=good, hypotheses=[hypo(ticket="T9")]),
        "leak": play_out(refuted_review=[{"ref": "T1 a2", "killed": "play", "lesson": f"合成：用户 {UID} 说的"}]),
    }[case]


@pytest.mark.parametrize("case,why", [("missing", "漏了"), ("streak", "escalate"), ("dead_play", "new_evidence"),
                                      ("too_many", "最多 3"), ("bad_ref", "T9 a1"), ("not_negative", "不是否定"),
                                      ("bad_killed", "mechanism / play"), ("missing_keys", "缺"),
                                      ("bad_ticket", "不是已开的业务票"), ("leak", "uid")])
def test_play_bad_output_rejected_whole(ap, case, why):
    (ap.tmp / "u").mkdir()
    (ap.tmp / "u" / "users.json").write_text(json.dumps({UID: {}}))
    ap.cfg["leak_guard_globs"] = [str(ap.tmp / "u" / "*.json")]
    store.save_state(ap.home / "state.json", _case_state(case))
    rid = start_play(ap)
    before = ap.state()
    d = _case_out(case)
    d["learned"] = ["合成事实"]
    finish(ap, rid, d)
    res = ap.run()
    st = ap.state()
    (item,) = res["analyst_done"]
    assert item["run"] == rid and item["failed"] is True and why in item["error"], item["error"]
    assert st["tickets"] == before["tickets"]                            # 整份拒：复盘结论一条都不落
    assert learned_rows(ap) == []                                        # learned 也不记


def test_play_escalate_goes_to_router_first(ap):
    store.save_state(ap.home / "state.json", _case_state("streak"))
    rid = start_play(ap)
    run = ap.state()["analyst"]["runs"][rid]
    assert run["streaks"] == [{"metric": "合成指标", "refs": ["T1 a1", "T1 a2", "T1 a3"]}]
    assert "合成指标" in prompt_of(ap) and "必须填 `escalate`" in prompt_of(ap)
    finish(ap, rid, play_out(refuted_review=[{"ref": f"T1 a{n}", "killed": "play", "lesson": "合成"} for n in (1, 2, 3)],
                             escalate="合成：连续三次否定，来的人和投放承诺对得上吗？"))
    res = ap.run()
    assert res["analyst_done"] == [{"run": rid, "mode": "play", "out": run["out"],
                                    "escalate": "合成：连续三次否定，来的人和投放承诺对得上吗？"}]
    assert all(a["refuted"]["killed"] == "play" for a in ap.state()["tickets"]["T1"]["attempts"])


def test_dead_play_can_come_back_with_new_evidence(ap):
    store.save_state(ap.home / "state.json", _case_state("dead_play"))
    rid = start_play(ap)
    finish(ap, rid, play_out(refuted_review=[{"ref": "T1 a2", "killed": "mechanism", "lesson": "合成"}],
                             hypotheses=[hypo(prior_refuted=["T1 a1"], new_evidence="合成：新来的一群人 N 人这样玩")]))
    res = ap.run()
    assert not res["analyst_done"][0].get("failed")


# ---- C：daily 的 experience / readable / funnel ---------------------------------------------------

FUNNEL = {"window": "合成窗口", "stages": [{"stage": "合成：新装", "this_week": {"n": 100, "pct": 100},
                                            "last_week": {"n": 90, "pct": 100}},
                                           {"stage": "合成：第一次互动", "this_week": {"n": 60, "pct": 60},
                                            "last_week": {"n": 45, "pct": 50}}]}


def _daily(ap, data, day=13):
    ap.ctx.clock = at(day, 1, 31)
    ap.run()
    rid = max(runs(ap, "daily"), key=lambda r: int(r.split("-")[1]))
    finish(ap, rid, data)
    return rid, ap.run()


def _v(**over):
    v = {"ticket": "T1", "attempt": 1, "verdict": "pending_sample", "touched": 4, "hit_wall": 2, "continued": 1,
         "note": "合成：不到 3 人", "new_wall": None}
    v.update(over)
    return v


def test_daily_stores_experience_readable_and_funnel(ap):
    ap.put(T1=biz_m([att(1, None, 12)]))
    readable = {"enough": False, "min_diff_points": 141, "basis": "合成：走到墙 2 人"}
    rid, res = _daily(ap, {"verdicts": [_v(experience="合成：2 人先看到…", readable=readable)], "walls": [],
                           "funnel": FUNNEL})
    st = ap.state()
    a = st["tickets"]["T1"]["attempts"][0]
    assert res["errors"] == [] and a["experience"] == "合成：2 人先看到…" and a["readable"] == readable
    f = st["analyst"]["funnel"]
    assert f["stages"] == FUNNEL["stages"] and f["window"] == "合成窗口" and f["run"] == rid and f["at"]
    _, res = _daily(ap, {"verdicts": [_v(note="合成：还是不到 3 人")], "walls": []}, day=14)   # 旧格式：没有这三项也照收
    st = ap.state()
    a = st["tickets"]["T1"]["attempts"][0]
    assert res["errors"] == [] and a["note"] == "合成：还是不到 3 人" and a["experience"] == "合成：2 人先看到…"
    assert st["analyst"]["funnel"]["run"] == rid                         # 没给 funnel：上一份留着


def test_daily_bad_funnel_reported_verdict_still_lands(ap):
    ap.put(T1=biz_m([att(1, None, 12)]))
    _, res = _daily(ap, {"verdicts": [_v()], "walls": [], "funnel": {"stages": []}})
    st = ap.state()
    assert len(res["errors"]) == 1 and "funnel" in res["errors"][0]
    assert st["tickets"]["T1"]["attempts"][0]["verdict"] == "pending_sample" and "funnel" not in st["analyst"]


def test_daily_experience_leak_drops_only_experience(ap):
    (ap.tmp / "u").mkdir()
    (ap.tmp / "u" / "users.json").write_text(json.dumps({UID: {}}))
    ap.cfg["leak_guard_globs"] = [str(ap.tmp / "u" / "*.json")]
    ap.put(T1=biz_m([att(1, None, 12)]))
    _, res = _daily(ap, {"verdicts": [_v(verdict="harmful", hit_wall=3, continued=0, note="合成：护栏变坏",
                                         experience=f"合成：{UID} 先看到…")], "walls": []})
    st = ap.state()
    a = st["tickets"]["T1"]["attempts"][0]
    assert a["verdict"] == "harmful" and "experience" not in a          # 有害的撤回不能被描述字段挡住
    assert res["rollback_needed"] == [{"ticket": "T1", "rollback": "关开关 x"}]
    assert len(res["errors"]) == 1 and "experience" in res["errors"][0]


# ---- C / D：status 和看板 --------------------------------------------------------------------------

def test_status_json_and_text_lead_with_product_readout(cli):
    s = make_state(T1=biz_m([att(1, "useless", 12, experience="经" * 150, readable={"enough": True})]))
    s["analyst"] = {"funnel": {**FUNNEL, "run": "daily-3", "at": "2026-11-13T01:40:00Z"}}
    store.save_state(cli.home / "state.json", s)
    for i in range(4):
        analyst.append_learned(cli.home, f"play-{i}", "play", [f"合成事实{i}"], f"2026-11-1{i}T00:00:00Z")
    rc, out = cli.run("status", "--json")
    assert rc == 0
    an = out["analyst"]
    assert an["funnel"]["stages"] == FUNNEL["stages"] and an["funnel"]["run"] == "daily-3"
    assert "合成：新装 100（100%），上周 90（100%）" in an["funnel_line"] and an["funnel_line"].startswith("合成窗口")
    assert [x["text"] for x in an["learned"]] == ["合成事实1", "合成事实2", "合成事实3"]
    a = out["tickets"]["T1"]["attempts"][0]
    assert a["experience"] == "经" * 150 and a["readable"] == {"enough": True}
    import subprocess
    import sys
    from test_analyst import CLI
    txt = subprocess.run([sys.executable, str(CLI), "--root", str(cli.root), "status"], capture_output=True,
                         text=True, timeout=30).stdout
    first = txt.splitlines()[0]
    assert first.startswith("产品读数：合成窗口") and "最近学到：合成事实3" in first


def test_status_without_readout(cli):
    store.save_state(cli.home / "state.json", make_state())
    rc, out = cli.run("status", "--json")
    assert out["analyst"] == {"funnel": None, "funnel_line": "还没有漏斗读数", "learned": []}


def test_board_funnel_learned_and_experience():
    now = datetime(2026, 11, 13, 2, 0, tzinfo=timezone.utc)
    s = make_state(T1=biz_m([att(1, "useless", 12, experience="甲" * 100 + "乙<b>")]))
    s["analyst"] = {"funnel": {**FUNNEL, "run": "daily-3", "at": "2026-11-13T01:40:00Z"}}
    learned = [{"run": f"play-{i}", "at": "2026-11-12T00:00:00Z", "mode": "play", "text": f"合成事实{i}<i>"}
               for i in range(4)]
    html = board.render(s, [], {}, now, learned=learned)
    top = html[html.index("<h2>产品漏斗</h2>"):html.index('<div class="stats">')]       # 顶上一栏，在统计之前
    for w in ("合成：新装", "100（100%）", "90（100%）", "合成：第一次互动", "60（60%）", "45（50%）", "合成窗口", "daily-3"):
        assert w in top, w
    assert "合成事实0" not in top and all(f"合成事实{i}&lt;i&gt;" in top for i in (1, 2, 3))   # 最近 3 条、转义
    assert "他们经历了：" + "甲" * 100 + "</small>" in html and "乙" not in html          # experience 只显示前 100 字
    empty = board.render(make_state(), [], {}, now)
    assert "还没有漏斗读数" in empty and "<table>" not in empty[empty.index("产品漏斗"):empty.index('class="stats"')]


# ---- CLI：analyst run --mode play、verdict --experience ------------------------------------------

def test_cli_analyst_run_play(cli):
    rc, out = cli.run("analyst", "run", "--mode", "play")
    assert rc == 0 and out["mode"] == "play" and not out["reused"]
    r = cli.state()["analyst"]["runs"][out["run"]]
    assert r["must_review"] == [] and r["streaks"] == [] and r["since"] < r["until"]
    end = time.time() + 20
    while not cli.claude() and time.time() < end:
        time.sleep(0.05)
    argv = cli.claude()[0]
    assert "玩法复盘" in argv[argv.index("-p") + 1]


def test_cli_verdict_takes_experience_and_readable(cli):
    store.save_state(cli.home / "state.json", make_state(T1=biz_m([att(1, None, 12)])))
    rc, out = cli.run("verdict", "T1", "--attempt", "1", "--verdict", "pending_sample", "--touched", "3",
                      "--hit-wall", "1", "--continued", "0", "--note", "合成", "--experience", "合成经历",
                      "--readable", "合成：读不出")
    assert rc == 0, out
    a = cli.state()["tickets"]["T1"]["attempts"][0]
    assert a["experience"] == "合成经历" and a["readable"] == "合成：读不出"


# ---- 文档：模板通用、负向反馈写进模板、玩法卡、汇报先讲产品 --------------------------------------------

def test_play_template_method_and_negative_feedback():
    import github
    text = (SKILL / "templates" / "analyst_play.md").read_text(encoding="utf-8")
    for w in ("投入最深", "照 brief", "完整读", "跨他所有的对话", "走掉的人", "怎么玩", "哪一刻上头", "我们设计的玩法",
              "差在哪", "最多 3 条", "改玩法，不是换个机制修卡点"):
        assert w in text, w
    for w in ("refuted_review", "`mechanism`", "`play`", "否定机制 ≠ 否定目标", "new_evidence", "prior_refuted",
              "escalate", "连续 3 次", "渠道", "投放", "指标选得对不对", "定位", "不许缩小", "更小、更安全",
              "判定仍由 daily 做", "$must_review", "$streaks", "$prior_plays", "$home/analyst/learned.jsonl",
              "$home/analyst/rejected.jsonl", "readable_in_days", "traffic_per_day", "cheapest_test"):
        assert w in text, w
    assert "只留本机" in text and "不写 uid" in text and "原话" in text
    for word in re.findall(r"[0-9A-Za-z_-]{6,}", text):                  # 公开仓：不出现 uid、项目专属的名字
        assert not github._is_uid(word), word
    for banned in ("aifriends", "her-", "girlfriend", "supercli", "persona"):
        assert banned not in text.lower(), banned
    assert "learned.jsonl" in (SKILL / "templates" / "analyst_weekly.md").read_text(encoding="utf-8")


def test_daily_template_experience_readable_funnel():
    text = (SKILL / "templates" / "analyst_daily.md").read_text(encoding="utf-8")
    for w in ("`experience`", "完整首段使用", "先看到什么", "哪一刻无聊或上头", "不抄原话", "`readable`",
              "能分辨的最小差别", "`funnel`", "照 brief 的漏斗口径", "新装 → 进入核心页 → 第一次互动 → 深度互动 → 次日回来"):
        assert w in text, w


def test_worker_card_leads_with_play_card_for_business():
    worker = (SKILL / "worker.md").read_text(encoding="utf-8")
    tpl = re.search(r"^##[^\n]*方向卡模板[^\n]*\n(.*?)(?=^## )", worker, re.S | re.M).group(1)
    i, j = tpl.find("**玩法卡**"), tpl.find("**产品诊断**")
    assert 0 <= i < j < tpl.find("1. 问题")                               # 业务票玩法卡打头；老票 / 维护票照旧产品诊断
    seg = tpl[i:j]
    for w in ("只有业务票", "替代", "他来要什么", "现在让他玩什么", "改成什么玩法", "为什么他会因此多用", "机制链",
              "预计提升几个点", "从多少到多少", "几天读得出", "每天多少新用户", "每组要多少人", "不写代码能不能先试",
              "换个机制修卡点", "第 3 项", "第 7 项", "第 9 项", "第 2、5、6 项"):
        assert w in seg, w
    assert "老票、维护票" in tpl[j:j + 40]
    biz_sec = _section(worker, "业务票")
    assert "玩法卡" in biz_sec
    assert "玩法卡" in (SKILL / "templates" / "brief_business.md").read_text(encoding="utf-8")


def test_direction_review_challenges_missing_play_card():
    text = (SKILL / "templates" / "direction_review.md").read_text(encoding="utf-8")
    q = text[text.index("9. "):]
    q = q[:q.index("\n")]
    for w in ("玩法卡", "换个机制修卡点", "【方向】", "读不出来", "一两周", "更大的改动", "合并成一个更大的尝试",
              "没有数据", "【细节】", "老票、维护票跳过"):
        assert w in q, w


def test_skill_play_protocol_and_product_first_reports():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    rows = {m.group(1): line for line in _section(text, "2.").splitlines()
            for m in [re.match(r"^\|\s*`([a-z_]+)`", line)] if m}
    row = rows["analyst_done"]
    for w in ("mode=play", "escalate", "不交假设", "≤ 3 条", "预计提升", "几天读得出", 'redirect <T> "<原话>" --as-user',
              "add --business"):
        assert w in row, w
    s3 = _section(text, "3.")
    play = next(l for l in s3.splitlines() if l.startswith("| play |"))
    for w in ("ISO 周", "weekly 跑完", "analyst run --mode play", "analyst_done", "learned", "refuted_review"):
        assert w in play, w
    for w in ("否定机制 ≠ 否定目标", "new_evidence", "连续 3 次", "不许把假设缩小", "learned.jsonl", "daily 做"):
        assert w in s3, w
    s4 = _section(text, "4.")
    assert "**方向卡先看玩法卡**" in s4 and "换个机制修卡点" in s4 and "合并成一个更大的尝试" in s4 and "【细节】" in s4
    assert "方向卡先看产品诊断" not in text
    s8 = _section(text, "8.")
    rep = s8[s8.index("### 给用户的汇报"):]
    for w in ("第一段固定是产品读数", "status --json", "analyst.funnel", "最近学到的一件", "工程进度压成一行"):
        assert w in rep, w
    assert "learned.jsonl" in _section(text, "10.")


def test_play_streak_skips_no_entry():
    """没入口 = 没人碰到改动，对玩法没有信息：不算进连败，也不打断连败；但照样要复盘。"""
    s = make_state(T1=biz_m([att(1, "useless", 10), att(2, "no_entry", 11), att(3, "harmful", 12)]))
    ctx = analyst.play_context(s, "2026-11-01")
    assert ctx["must_review"] == ["T1 a1", "T1 a2", "T1 a3"] and ctx["streaks"] == []
    s = make_state(T1=biz_m([att(1, "useless", 10), att(2, "no_entry", 11), att(3, "harmful", 12),
                             att(4, "useless", 13)]))
    ctx = analyst.play_context(s, "2026-11-01")
    assert ctx["streaks"] == [{"metric": "合成指标", "refs": ["T1 a1", "T1 a3", "T1 a4"]}]
