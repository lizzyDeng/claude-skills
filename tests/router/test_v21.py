"""router v2.1（用户 2026-11-13 确认的 8 条改动）。夹具全是合成文本，不含真实 uid 和原话。"""
import json

from test_business_cli import _A1, _biz, _card, _push_q, _verdict
from test_router_cli import proj, push_main, seed, wait_until  # noqa: F401  (proj 是 fixture)


def _items(proj, tid="T1", **match):
    return [q for q in proj.state()["queue"] if q.get("ticket") == tid and all(q.get(k) == v for k, v in match.items())]


def _inbox(proj, tid="T1"):
    return json.dumps(json.loads(proj.run("inbox", "--ticket", tid).stdout), ensure_ascii=False)


# ---- 第 1 条：业务票方向卡交用户确认，取消自动确认；router 不再自己换方向 ----------------------------------

def test_v21_1_business_card_goes_to_user(proj):
    _biz(proj, status="kickoff", card_confirmed=False)
    c = _card(proj)
    out = proj.out("submit-card", "T1", "--card", str(c), "--rounds", "1")
    s = proj.state(); t = s["tickets"]["T1"]
    (q,) = _items(proj, kind="card")
    assert out["queue"] == q["id"] and not out.get("auto_confirmed")
    assert t["status"] == "await_card" and not t.get("card_confirmed") and proj.claude() == []
    assert not (proj.home / "decisions.md").exists() or "票级授权" not in (proj.home / "decisions.md").read_text()
    out = proj.out("answer", q["id"], "同意")                                     # 用户回「同意」照现有 answer 流程
    t = proj.state()["tickets"]["T1"]
    assert out["resumed"] == "T1" and t["card_confirmed"] is True and t["card_confirmed_by"] == "user"
    assert t["status"] == "executing"
    wait_until(lambda: proj.claude())
    prompt = proj.claude()[0][proj.claude()[0].index("-p") + 1]
    assert "用户原话（逐字）" in prompt and "同意" in prompt and "router 代为确认" not in prompt


def test_v21_1_business_card_reject_and_change(proj):
    _biz(proj, status="kickoff", card_confirmed=False)
    qid = proj.out("submit-card", "T1", "--card", str(_card(proj)))["queue"]
    out = proj.run("answer", qid, "驳回：合成理由", "--reject")
    t = proj.state()["tickets"]["T1"]
    assert json.loads(out.stdout)["ok"] and t["status"] == "kickoff" and not t.get("card_confirmed")
    assert "card_confirmed_by" not in t


def test_v21_1_as_router_cannot_confirm_card_or_choose_direction(proj):
    _biz(proj, status="kickoff", card_confirmed=False)
    qid = proj.out("submit-card", "T1", "--card", str(_card(proj)))["queue"]
    r = proj.run("answer", qid, "同意", "--as-router", check=False)
    assert r.returncode == 1 and "用户" in json.loads(r.stdout)["error"]
    assert qid in [q["id"] for q in proj.state()["queue"]] and proj.claude() == []
    _biz(proj, status="postlaunch", attempts=[{**_A1[0], "verdict": "useful"}])
    for topic in ("next", "assumption"):                                          # 问下一个方向 / 核心假设被推翻：归用户
        st = proj.state()
        st["seq"]["queue"] += 1
        q2 = f"Q{st['seq']['queue']}"
        st["queue"].append({"id": q2, "ticket": "T1", "kind": "direction", "topic": topic, "summary": "合成",
                            "created_at": "x", "notified_at": "x"})
        (proj.home / "state.json").write_text(json.dumps(st, ensure_ascii=False))
        r = proj.run("answer", q2, "合成方向", "--as-router", check=False)
        assert r.returncode == 1, topic
        assert q2 in [q["id"] for q in proj.state()["queue"]]


def test_v21_1_redirect_as_router_refused_new_card_requeues(proj):
    _biz(proj, attempts=_A1)
    _verdict(proj, "harmful")
    r = proj.run("redirect", "T1", "合成方向乙", "--as-router", check=False)
    out = json.loads(r.stdout)
    assert r.returncode == 1 and "--new-card" in out["error"] and "--as-user" in out["error"]
    assert proj.state()["tickets"]["T1"]["status"] == "postlaunch"
    r = proj.run("redirect", "T1", "合成方向乙", "--new-card", check=False)        # 出新卡不带方向：方向由 worker 提、用户定
    assert r.returncode == 1
    out = proj.out("redirect", "T1", "--new-card")
    t = proj.state()["tickets"]["T1"]
    assert out["ok"] and t["status"] == "queued" and t["card_confirmed"] is False
    assert not _items(proj, kind="harm") and proj.claude() == []
    inbox = _inbox(proj)
    assert "新方向卡" in inbox and "用户" in inbox and "router 决定" not in inbox
    dec = (proj.home / "decisions.md").read_text()
    assert "出新方向卡" in dec
    assert proj.run("redirect", "T1", check=False).returncode == 2                  # 来源必须写明
    assert proj.run("redirect", "T1", "--as-user", check=False).returncode == 1     # 用户定方向要有原话


def test_v21_1_new_card_refused_while_return_unanswered(proj):
    _biz(proj, attempts=_A1, streak=2)
    _verdict(proj)                                                               # 第 3 次没用 ⇒ return
    r = proj.run("redirect", "T1", "--new-card", check=False)
    assert r.returncode == 1 and "--as-user" in json.loads(r.stdout)["error"]
    assert proj.state()["tickets"]["T1"]["no_use_streak"] == 3
    proj.out("redirect", "T1", "合成用户方向", "--as-user")
    assert proj.state()["tickets"]["T1"]["no_use_streak"] == 0


def test_v21_1_next_item_new_card_clears_it(proj):
    _biz(proj, attempts=[{**_A1[0], "verdict": "useful"}], walls=0, waiting_for="墙已全部拆完，等分析 agent 提议结束")
    cq = _push_q(proj, "T1", "close")
    proj.out("answer", cq, "不关")
    (q,) = _items(proj, topic="next")
    assert "用户" in q["summary"] and "router 定" not in q["summary"]
    out = proj.out("redirect", "T1", "--new-card")
    assert q["id"] in out["cleared"] and proj.state()["tickets"]["T1"]["status"] == "queued"


def _ready_biz(**extra):
    return ("await_prod", [], {"kind": "business", "authorized": True, "title": "业务票", "attempts": [],
                               "card_confirmed": True, **extra})


def test_v21_1_prod_batch_router_confirmed_card_needs_release(proj):
    """v2 时代 router 代确认的方向卡（没有 card_confirmed_by=user）：router 不自己上 prod，整批交用户放行。"""
    push_main(proj.root)
    seed(proj, T2=_ready_biz(solution={"kind": "card", "text": "合成", "source": "方向卡 v1（已确认，票级授权）"}))
    out = proj.out("prod-batch")
    assert out["router_ships"] == [] and out["queue"]
    assert {b["ticket"]: b["state"] for b in out["batch"]} == {"T2": "方向卡没经你确认"}
    proj.out("answer", out["queue"], "放行")
    assert proj.out("prod-batch")["router_ships"] == ["T2"]
    seed(proj, T2=_ready_biz(card_confirmed_by="user"))                           # 用户确认过的方案：router 照常上
    out = proj.out("prod-batch")
    assert out["router_ships"] == ["T2"] and out["queue"] is None


def test_v21_1_assumption_broken_item_is_users(proj):
    from conftest import make_state
    import events
    wt = proj.tmp / "wt"; wt.mkdir()
    st = make_state(T1=("executing", ["her-reply-prompt"], {
        "kind": "business", "authorized": True, "title": "业务票", "attempts": [], "walls": [], "card_confirmed": True,
        "worker": {"session_id": "sid", "worktree": str(wt), "runs": 1, "pid": 999999, "log": str(proj.tmp / "w.log"),
                   "started_at": "2026-11-01T00:00:00Z"}}))
    (proj.home / "state.json").write_text(json.dumps(st, ensure_ascii=False))
    events.emit(proj.home, "T1", "assumption_broken", "合成：核心假设甲不成立", "合成证据", at="2026-11-02T00:00:00Z")
    proj.out("tick")
    (q,) = _items(proj, kind="direction")
    assert q.get("topic") == "assumption"
    r = proj.run("answer", q["id"], "合成", "--as-router", check=False)
    assert r.returncode == 1


def test_v21_1_docs_card_and_scope():
    from test_docs import SKILL, _section
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    s4, s5 = _section(text, "4."), _section(text, "5.")
    assert "自动确认" not in text.replace("不再自动确认", "")                      # 只剩「不再自动确认」的说明
    assert "不再自动确认" in s4 and "交用户确认" in s4
    user_rows = [l for l in s5.splitlines() if l.startswith("|") and l.rstrip().endswith("**用户**")
                 or "| **用户**" in l]
    joined = "\n".join(user_rows)
    for w in ("方向卡", "上线节奏", "成本"):
        assert w in joined, w
    router_row = next(l for l in s5.splitlines() if "票级授权" in l and "**你**" in l)
    for w in ("合 staging", "用户确认过的方案", "撤回", "查入口", "验收口径", "代码面"):
        assert w in router_row, w
    assert "redirect --new-card" in text and "--as-router" in _section(text, "6.")
    assert "--as-router` 一律拒" in text or "--as-router 一律拒" in text
    for doc in ("worker.md", "templates/brief_business.md"):
        body = (SKILL / doc).read_text(encoding="utf-8")
        assert "代为确认" not in body and "交用户确认" in body, doc


# ---- 第 2 条（+ 补充 A）：一个业务目标一张票，卡点是票里的墙；墙是根因不是表象；业务票能并进另一张业务票 ----------

def _bt(status="postlaunch", **extra):
    """合成业务票（make_state 的规格）：两道墙，可带尝试。"""
    base = {"kind": "business", "authorized": True, "title": "合成业务票", "issue": None, "card_confirmed": True,
            "segment": "合成人群", "want": "合成想要", "metric": {"name": "合成指标"}, "evidence": [],
            "impact": {}, "absorbs": [], "attempts": [], "no_use_streak": 0, "tasks": [], "parent": None,
            "walls": [{"id": "W1", "text": "墙1", "status": "open", "source": "analyst", "at": "x"},
                      {"id": "W2", "text": "墙2", "status": "open", "source": "analyst", "at": "x"}]}
    base.update(extra)
    return (status, ["her-reply-prompt"], base)


def _seed2(proj, **tickets):
    from conftest import make_state
    st = make_state(**tickets)
    (proj.home / "state.json").write_text(json.dumps(st, ensure_ascii=False))
    return st


def _merge(proj, target="T1", source="T2", *extra, check=True):
    r = proj.run("merge", target, source, "--said", "合成：T2 并进 T1", *extra, check=check)
    return json.loads(r.stdout) if check else r


_PEND = {"n": 1, "direction": "合成方向乙", "at": "2026-11-01T00:00:00Z", "rollback": "合成：关开关乙", "verdict": None}
_DONE1 = {"n": 1, "direction": "合成方向甲", "at": "2026-10-20T00:00:00Z", "rollback": "合成：关开关甲",
          "verdict": "useful", "wall": "W1"}


def test_v21_2_merge_moves_walls_evidence_attempts_and_marks_source(proj):
    _seed2(proj, T1=_bt(attempts=[dict(_DONE1)], evidence=["合成证据甲"],
                        walls=[{"id": "W1", "text": "墙1", "status": "broken", "source": "analyst", "at": "x"},
                               {"id": "W2", "text": "墙2", "status": "open", "source": "analyst", "at": "x"}]),
           T2=_bt(title="合成业务票二", evidence=["合成证据乙", "合成证据甲"],
                  attempts=[{**_DONE1, "direction": "合成方向丙", "verdict": "useless", "wall": "W1"}]))
    out = proj.out("merge", "T1", "T2", "--said", "合成：T2 并进 T1")
    s = proj.state(); t1, t2 = s["tickets"]["T1"], s["tickets"]["T2"]
    assert out["walls"] == {"W1": "W3", "W2": "W4"} and out["attempts"] == {"1": 2}
    assert [w["id"] for w in t1["walls"]] == ["W1", "W2", "W3", "W4"] and t1["walls"][2]["from"] == "T2/W1"
    assert t1["evidence"] == ["合成证据甲", "合成证据乙"]
    (moved,) = [a for a in t1["attempts"] if a.get("merged_from")]
    assert moved["n"] == 2 and moved["merged_from"] == "T2" and moved["orig_n"] == 1 and moved["wall"] == "W3"
    assert t1["attempts"][-1]["n"] == 1                                              # 判完的并入尝试排前面：当前尝试还是 T1 自己的
    assert t2["status"] == "done" and t2["merged_into"] == "T1" and t2["walls"] == [] and t2["attempts"] == []
    assert "已并入 T1" in t2["waiting_for"]
    assert "并入 T2" in (proj.home / "decisions.md").read_text()
    assert "合成：T2 并进 T1" in (proj.home / "answers.md").read_text()
    assert "已并入 T1" in (proj.home / "board.html").read_text()
    assert "已并入 T1" in proj.run("status").stdout
    import analyst
    assert "合成业务票二" not in analyst.state_summary(proj.state(), "daily")      # 已并的票不再判、不再派
    proj.out("tick")
    assert proj.state()["tickets"]["T2"]["status"] == "done" and not proj.claude()


def test_v21_2_merge_inflight_attempt_daily_verdict_lands_on_target(proj):
    """被并票带着已上线待判的尝试：判定照常归到目标票，daily 写到目标票不报错，拆的是被并过来的那道墙。"""
    _seed2(proj, T1=_bt(attempts=[dict(_DONE1)],
                        walls=[{"id": "W1", "text": "墙1", "status": "broken", "source": "analyst", "at": "x"},
                               {"id": "W2", "text": "墙2", "status": "open", "source": "analyst", "at": "x"}]),
           T2=_bt(title="合成业务票二", attempts=[dict(_PEND)]))
    out = _merge(proj)
    t1 = proj.state()["tickets"]["T1"]
    n = out["attempts"]["1"]
    assert t1["attempts"][-1]["n"] == n and t1["attempts"][-1]["verdict"] is None
    assert t1["attempts"][-1]["wall"] == "W3"                                      # 它当时对准的是 T2 的 W1
    assert f"尝试 {n}" in t1["waiting_for"]
    import analyst
    assert f"尝试 {n}「合成方向乙」" in analyst.state_summary(proj.state(), "daily")
    d = {"verdicts": [{"ticket": "T1", "attempt": n, "verdict": "useful", "touched": 4, "hit_wall": 3, "continued": 3,
                       "note": "合成：3 人走到墙，墙没了"}]}
    p = proj.tmp / "d.json"; p.write_text(json.dumps(d, ensure_ascii=False))
    res = proj.out("apply-daily", "--from", str(p))
    assert res["errors"] == [] and len(res["applied"]) == 1
    t1 = proj.state()["tickets"]["T1"]
    assert {w["id"]: w["status"] for w in t1["walls"]} == {"W1": "broken", "W2": "open", "W3": "broken", "W4": "open"}
    assert t1["status"] == "queued"                                               # 目标票空闲：照常拆下一道墙
    r = proj.run("verdict", "T2", "--attempt", "1", "--verdict", "useful", "--touched", "1", "--hit-wall", "1",
                 "--continued", "1", "--note", "合成", check=False)
    assert r.returncode == 1                                                      # 被并票不再收判定


def test_v21_2_merge_two_inflight_both_judged_on_target(proj):
    _seed2(proj, T1=_bt(attempts=[{**_PEND, "direction": "合成方向甲", "wall": "W1"}]),
           T2=_bt(title="合成业务票二", attempts=[dict(_PEND)]))
    out = _merge(proj)
    n2 = out["attempts"]["1"]
    import analyst
    summ = analyst.state_summary(proj.state(), "daily")
    assert "尝试 1「合成方向甲」" in summ and f"尝试 {n2}「合成方向乙」" in summ and "并自 T2" in summ
    first = json.loads(_verdict(proj, "useful", 1).stdout)                        # 还有一个在等判定：先不重新排队
    t1 = proj.state()["tickets"]["T1"]
    assert "deferred" in first["actions"] and t1["status"] == "postlaunch" and t1["walls"][0]["status"] == "broken"
    assert proj.run("released", "--tickets", "T1", "--what", "x", "--metrics", "m", "--rollback", "r",
                    check=False).returncode == 1                                  # 一次只验一个：还有待判的不许上新的
    r = proj.run("redirect", "T1", "--new-card", check=False)
    assert r.returncode == 1 and "在途" in json.loads(r.stdout)["error"]            # 还有待判：不换方向
    second = json.loads(_verdict(proj, "useless", n2).stdout)
    assert "change_direction" in second["actions"] and proj.state()["tickets"]["T1"]["status"] == "queued"


def test_v21_2_merge_into_busy_target_defers_requeue(proj):
    wt = proj.tmp / "wt1"; wt.mkdir()
    _seed2(proj, T1=_bt(status="executing", worker={"session_id": "s1", "worktree": str(wt), "runs": 1}),
           T2=_bt(title="合成业务票二", attempts=[dict(_PEND)]))
    _merge(proj)
    t1 = proj.state()["tickets"]["T1"]
    n = t1["attempts"][-1]["n"]
    assert t1["status"] == "executing"
    out = json.loads(_verdict(proj, "useless", n).stdout)
    t1 = proj.state()["tickets"]["T1"]
    assert "deferred" in out["actions"] and t1["status"] == "executing" and t1["worker"]["session_id"] == "s1"
    assert "合成备注" in _inbox(proj)                                               # 判定转给在做的 worker


def test_v21_2_merge_refusals(proj):
    import subprocess
    _seed2(proj, T1=_bt(), T2=_bt(status="executing"), T3=("postlaunch", [], {"title": "老票"}),
           T4=_bt(status="await_prod", merge_sha="abc", history=[{"at": "x", "status": "await_prod"}]),
           T5=_bt(status="paused", pr=55), T6=_bt(status="done", merged_into="T1"))
    for args in (("T1", "T1"), ("T1", "T2"), ("T1", "T3"), ("T3", "T1"), ("T1", "T4"), ("T1", "T5"), ("T1", "T6"),
                 ("T6", "T1"), ("T1", "T99")):
        r = _merge(proj, *args, check=False)
        assert r.returncode == 1, args
    assert proj.run("merge", "T1", "T2", check=False).returncode == 2                # --said 必填（用户原话）
    assert proj.run("merge", "T1", "T2", "--said", "x", env={"ROUTER_TICKET": "T1"}, check=False).returncode == 2
    assert proj.state()["tickets"]["T2"]["status"] == "executing"


def test_v21_2_merge_walls_first_and_harm_item_moves(proj):
    _seed2(proj, T1=_bt(), T2=_bt(title="合成业务票二", attempts=[{**_PEND, "verdict": "harmful", "wall": "W1"}]))
    st = proj.state()
    st["seq"]["queue"] = 5
    st["queue"] = [{"id": "Q4", "ticket": "T2", "kind": "harm", "summary": "T2 尝试 1 有害，需撤回：合成", "created_at": "x",
                    "notified_at": "x"},
                   {"id": "Q5", "ticket": "T2", "kind": "close", "summary": "合成", "created_at": "x", "notified_at": "x"}]
    (proj.home / "state.json").write_text(json.dumps(st, ensure_ascii=False))
    out = _merge(proj, "T1", "T2", "--walls-first")
    s = proj.state()
    assert [w["text"] for w in s["tickets"]["T1"]["walls"]] == ["墙1", "墙2", "墙1", "墙2"]
    assert [w.get("from") for w in s["tickets"]["T1"]["walls"]][:2] == ["T2/W1", "T2/W2"]
    assert [(q["id"], q["ticket"]) for q in s["queue"]] == [("Q4", "T1")]          # 有害待撤回挪过去；其余作废
    assert out["moved_items"] == ["Q4"] and out["dropped_items"] == ["Q5"]


def test_v21_2_merge_reparents_absorbed_legacy(proj):
    from conftest import make_state
    st = make_state(T1=_bt(attempts=[dict(_DONE1)]),
                    T2=_bt(title="合成业务票二", absorbs=["T5"],
                           attempts=[{"n": 1, "direction": "老票五", "shipped_at": "x", "verdict": "legacy",
                                      "legacy_verdict": "没用", "historical": True}]),
                    T5=("postlaunch", [], {"title": "老票五", "parent": "T2", "parent_attempt": 1}))
    (proj.home / "state.json").write_text(json.dumps(st, ensure_ascii=False))
    out = _merge(proj)
    s = proj.state()
    assert s["tickets"]["T5"]["parent"] == "T1" and s["tickets"]["T5"]["parent_attempt"] == out["attempts"]["1"]
    assert s["tickets"]["T1"]["absorbs"] == ["T5"]
    hist = [a for a in s["tickets"]["T1"]["attempts"] if a.get("historical")]
    assert len(hist) == 1 and s["tickets"]["T1"]["attempts"][-1]["n"] == 1        # 历史尝试排在 live 尝试之前


def test_v21_2_attempt_breaks_its_own_wall(proj):
    """released 记下这次尝试对准的墙；判定拆的是那一道（不是判定那一刻的第一道没拆的墙）。"""
    _seed2(proj, T1=_bt(status="await_prod"))
    proj.out("released", "--tickets", "T1", "--what", "合成", "--metrics", "m", "--rollback", "合成：关开关")
    assert proj.state()["tickets"]["T1"]["attempts"][0]["wall"] == "W1"


def test_v21_2_propose_flags_same_metric_and_skips_small(proj):
    from test_business_cli import load, put
    d = load(maint=[])
    d["problems"][1]["metric"] = dict(d["problems"][0]["metric"])                  # 两条同一指标
    out = proj.out("propose", "--from", put(proj, d))
    s = proj.state()
    p1, p2 = s["proposals"]["P1"], s["proposals"]["P2"]
    assert p1["same_metric"] == ["P2"] and p2["same_metric"] == ["P1"]
    vq = [q for q in s["queue"] if q["kind"] == "verify_problem"]
    assert all("同指标" in q["summary"] for q in vq)
    d = load(maint=[])
    d["problems"][0]["key"], d["problems"][1]["key"] = "k8", "k9"
    d["problems"][1]["impact"] = {"size": 0.05, "gap": 0.9, "confidence": "high"}   # 人群 < 10%：不成问题
    out = proj.out("propose", "--from", put(proj, d))
    assert [p["key"] for p in out["proposals"]] == ["k8"] and out["skipped_small"] == ["k9"]


def test_v21_2_templates_one_goal_one_ticket_and_root_cause_walls():
    from test_docs import SKILL, _section
    for mode in ("weekly", "calibrate", "migrate"):
        text = (SKILL / "templates" / f"analyst_{mode}.md").read_text(encoding="utf-8")
        for w in ("业务目标", "同一指标", "只出一条", "新用户不开口", "开口几句就走", "聊到第 10 句", "不能拆两条",
                  "自下而上", "在哪一刻走", "表象", "根因", "10%", "notes", "解释了多少", "占比"):
            assert w in text, (mode, w)
    rev = (SKILL / "templates" / "direction_review.md").read_text(encoding="utf-8")
    assert "表象" in rev and "小群体" in rev
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "按根归并" in _section(skill, "3.") and "router.py merge" in skill
    s6 = _section(skill, "6.")
    assert "router.py merge" in s6 and "--said" in s6
