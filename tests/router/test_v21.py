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

