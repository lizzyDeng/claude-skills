import json

from conftest import FIX
from test_router_cli import proj, push_main, seed, wait_until  # noqa: F401  (proj 是 fixture)


def load(**over):
    d = json.loads((FIX / "proposals.json").read_text())
    d.update(over)
    return d


def put(proj, data, name="p.json"):
    p = proj.tmp / name
    p.write_text(json.dumps(data, ensure_ascii=False))
    return str(p)


def propose(proj, data, check=True):
    return proj.run("propose", "--from", put(proj, data), check=check)


def calibrate(proj):
    import store
    path = proj.home / "state.json"
    s = json.loads(path.read_text()) if path.exists() else store.empty_state()
    s.setdefault("analyst", {})["calibrated"] = True
    path.write_text(json.dumps(s, ensure_ascii=False))


def test_propose_pushes_verify_items(proj):
    out = json.loads(propose(proj, load()).stdout)
    s = proj.state()
    assert out["ok"] and len(out["proposals"]) == 2 and out["skipped"] == []
    assert set(s["proposals"]) == {"P1", "P2"}
    vq = [q for q in s["queue"] if q["kind"] == "verify_problem"]
    assert [q["proposal"] for q in vq] == ["P1", "P2"]
    assert "maint_pending" not in out and len(out["maint"]) == 1      # R3：维护条目建成票
    mt = s["tickets"][out["maint"][0]]
    assert mt["kind"] == "maint" and mt["tasks"][0]["text"] == "测试维护甲：测试问题" and mt["issue"]


def test_propose_refused_before_calibration(proj):
    r = propose(proj, load(mode="weekly"), check=False)
    out = json.loads(r.stdout)
    assert r.returncode == 1 and not out["ok"] and "校准未过" in out["error"]
    assert not (proj.home / "state.json").exists() or not proj.state().get("proposals")
    calibrate(proj)
    assert json.loads(propose(proj, load(mode="weekly")).stdout)["ok"]


def test_propose_leak_refused(proj):
    (proj.runs / "2026-10-26" / "users.json").write_text(json.dumps({"deadbeef": {}}))
    d = load()
    d["problems"][1]["evidence"] = ["见用户 deadbeef"]
    r = propose(proj, d, check=False)
    out = json.loads(r.stdout)
    assert r.returncode == 1 and not out["ok"] and "deadbeef" in json.dumps(out)
    s = proj.state() if (proj.home / "state.json").exists() else {}
    assert not s.get("proposals") and not s.get("queue")


def test_propose_rejects_bad_impact(proj):
    for bad in ({"size": 1.5, "gap": 0.5, "confidence": "mid"},
                {"size": 0.5, "gap": "x", "confidence": "mid"},
                {"size": 0.5, "gap": 0.5, "confidence": "huge"},
                {"size": 0.5, "gap": 0.5}):
        d = load()
        d["problems"][1]["impact"] = bad
        r = propose(proj, d, check=False)
        out = json.loads(r.stdout)
        assert r.returncode == 1 and not out["ok"] and "impact" in out["error"], bad
    s = proj.state() if (proj.home / "state.json").exists() else {}
    assert not s.get("proposals") and not s.get("queue")


def test_propose_skips_rejected_key(proj):
    (proj.home / "analyst").mkdir()
    (proj.home / "analyst" / "rejected.jsonl").write_text(
        json.dumps({"key": "k1", "title": "t", "reason": "r", "at": "x"}) + "\n")
    out = json.loads(propose(proj, load()).stdout)
    assert [p["key"] for p in out["proposals"]] == ["k2"] and out["skipped"] == ["k1"]
    d = load()
    d["problems"][0]["new_evidence"] = "测试新证据"
    out = json.loads(propose(proj, d).stdout)
    assert "k1" in [p["key"] for p in out["proposals"]]


def _answer(proj, qid, text, check=True):
    return proj.run("answer", qid, text, check=check)


def _first_q(proj):
    return [q for q in proj.state()["queue"] if q["kind"] == "verify_problem"][0]["id"]


def test_answer_true_creates_and_starts(proj):
    propose(proj, load())
    qid = _first_q(proj)
    out = json.loads(_answer(proj, qid, "真").stdout)
    s = proj.state()
    t = s["tickets"][out["ticket"]]
    assert t["kind"] == "business" and t["authorized"] is True and t["origin"] == "analyst"
    assert t["issue"] and t["start_ok"] and t["status"] in ("queued", "kickoff")
    assert [c for c in proj.gh() if c[:2] == ["issue", "create"]]
    assert qid not in [q["id"] for q in s["queue"]]


def test_answer_true_hold(proj):
    propose(proj, load(maint=[]))              # 维护条目会自动 start、建 issue，这里只看判真的「先放着」
    for sep in ("，", ","):
        qid = _first_q(proj)
        out = json.loads(_answer(proj, qid, f"真{sep}先放着").stdout)
        t = proj.state()["tickets"][out["ticket"]]
        assert t["kind"] == "business" and t["status"] == "draft" and t["issue"] is None
    assert not [c for c in proj.gh() if c[:2] == ["issue", "create"]]


def test_answer_not_requires_reason(proj):
    propose(proj, load())
    qid = _first_q(proj)
    r = _answer(proj, qid, "不是", check=False)
    assert r.returncode == 1 and "不是：理由" in json.loads(r.stdout)["error"]
    assert qid in [q["id"] for q in proj.state()["queue"]]          # 没回成功：项还在
    assert json.loads(_answer(proj, qid, "不是：样本是测试机").stdout)["ok"]
    rows = [json.loads(l) for l in (proj.home / "analyst" / "rejected.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["key"] == "k1" and rows[0]["reason"] == "样本是测试机"
    assert set(rows[0]) == {"key", "title", "reason", "at"}


def test_answer_garbage_rejected(proj):
    propose(proj, load())
    r = _answer(proj, _first_q(proj), "好的", check=False)
    out = json.loads(r.stdout)
    assert r.returncode == 1 and "真" in out["error"] and "先放着" in out["error"] and "不是：理由" in out["error"]
    assert qid_in_queue(proj)


def qid_in_queue(proj):
    return any(q["kind"] == "verify_problem" for q in proj.state()["queue"])


def test_weekly_walls_attach_and_close_queues(proj):
    propose(proj, load())
    t1 = json.loads(_answer(proj, _first_q(proj), "真，先放着").stdout)["ticket"]
    calibrate(proj)
    d = load(mode="weekly", problems=[], maint=[], walls=[{"ticket": t1, "text": "测试墙丁"}],
             close=[{"ticket": t1, "reason": "测试结束理由"}])
    out = json.loads(propose(proj, d).stdout)
    s = proj.state()
    assert out["ok"] and s["tickets"][t1]["walls"][-1]["text"] == "测试墙丁"
    assert s["tickets"][t1]["walls"][-1]["source"] == "weekly"
    cq = [q for q in s["queue"] if q["kind"] == "close"]
    assert len(cq) == 1 and cq[0]["ticket"] == t1
    r = _answer(proj, cq[0]["id"], "好", check=False)
    assert r.returncode == 1
    out = json.loads(_answer(proj, cq[0]["id"], "关").stdout)
    s = proj.state()
    assert s["tickets"][t1]["status"] == "done"
    assert s["tickets"][t1]["closeout"] == {"downstream": {}, "issue": "n/a", "worktree": "pending", "branch": "pending"}
    assert "关闭业务票" in (proj.home / "decisions.md").read_text()


def test_close_answer_keep(proj):
    propose(proj, load())
    t1 = json.loads(_answer(proj, _first_q(proj), "真，先放着").stdout)["ticket"]
    calibrate(proj)
    propose(proj, load(mode="weekly", problems=[], maint=[], close=[{"ticket": t1, "reason": "r"}]))
    cq = [q for q in proj.state()["queue"] if q["kind"] == "close"][0]["id"]
    assert json.loads(_answer(proj, cq, "不关").stdout)["ok"]
    assert proj.state()["tickets"][t1]["status"] == "draft"


def test_absorbs_links_legacy(proj):
    s = seed(proj, T5=("postlaunch", [], {"title": "老票五", "postlaunch": {
        "t0": "2026-11-01T00:00:00Z", "checks": [],
        "results": [{"check": "T+1", "verdict": "没用"}]}}),
        T6=("draft", [], {"title": "老票六"}))
    d = load()
    d["problems"][0]["absorbs"] = ["T5", "T6"]
    propose(proj, d)
    out = json.loads(_answer(proj, _first_q(proj), "真，先放着").stdout)
    s = proj.state()
    new = s["tickets"][out["ticket"]]
    assert s["tickets"]["T5"]["parent"] == out["ticket"] and s["tickets"]["T6"]["parent"] == out["ticket"]
    assert new["attempts"] == [        # 只有已上线并验收过的老票算历史尝试；草稿 T6 只挂 parent
        {"n": 1, "direction": "老票五", "shipped_at": "2026-11-01T00:00:00Z", "verdict": "legacy",
         "legacy_verdict": "没用", "historical": True}]


def test_add_business_is_user_verified(proj):
    out = proj.out("add", "--business", "--title", "测试直提", "--segment", "测试人群甲", "--want", "测试想要",
                   "--wall", "测试墙甲", "--wall", "测试墙乙", "--metric", "测试指标")
    t = proj.state()["tickets"][out["ticket"]]
    assert t["kind"] == "business" and t["origin"] == "user" and t["authorized"] is True
    assert [w["text"] for w in t["walls"]] == ["测试墙甲", "测试墙乙"] and t["status"] == "draft"


def test_absorbs_unknown_id_rejects_whole_file(proj):
    d = load()
    d["problems"][0]["absorbs"] = ["T99"]
    r = propose(proj, d, check=False)
    assert r.returncode == 1 and "T99" in json.loads(r.stdout)["error"]


def _biz_closeable(proj, **extra):
    propose(proj, load())
    t1 = json.loads(_answer(proj, _first_q(proj), "真，先放着").stdout)["ticket"]
    if extra:
        s = proj.state(); s["tickets"][t1].update(extra)
        (proj.home / "state.json").write_text(json.dumps(s, ensure_ascii=False))
    return t1


def test_close_proposal_on_legacy_ticket_rejected(proj):
    seed(proj, T1=("draft", [], {"title": "老票"}))
    r = propose(proj, load(mode="migrate", problems=[], maint=[], close=[{"ticket": "T1", "reason": "r"}]), check=False)
    assert r.returncode == 1 and "T1" in json.loads(r.stdout)["error"]
    assert not proj.state()["queue"]


def test_close_answer_refused_while_worker_alive(proj):
    import subprocess, sys
    t1 = _biz_closeable(proj)
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        s = proj.state(); s["tickets"][t1]["worker"]["pid"] = p.pid
        (proj.home / "state.json").write_text(json.dumps(s))
        propose(proj, load(mode="migrate", problems=[], maint=[], close=[{"ticket": t1, "reason": "r"}]))
        cq = [q for q in proj.state()["queue"] if q["kind"] == "close"][0]["id"]
        r = _answer(proj, cq, "关", check=False)
        assert r.returncode == 1 and "stop" in json.loads(r.stdout)["error"]
        s = proj.state()
        assert s["tickets"][t1]["status"] == "draft" and cq in [q["id"] for q in s["queue"]]
    finally:
        p.kill(); p.wait()


def test_answer_prefix_is_anchored(proj):
    propose(proj, load(close=[]))
    vq = _first_q(proj)
    for bad in ("真的吗？", "真先放着吧", "不是理由", "不是：", "关于这个"):
        r = _answer(proj, vq, bad, check=False)
        out = json.loads(r.stdout)
        assert r.returncode == 1 and "格式" in out["error"], bad
        assert vq in [q["id"] for q in proj.state()["queue"]]
    t1 = json.loads(_answer(proj, vq, "真，先放着。").stdout)["ticket"]
    calibrate(proj)
    propose(proj, load(mode="weekly", problems=[], maint=[], close=[{"ticket": t1, "reason": "r"}]))
    cq = [q for q in proj.state()["queue"] if q["kind"] == "close"][0]["id"]
    for bad in ("关于这个先不关", "关注一下", "不关闭"):
        r = _answer(proj, cq, bad, check=False)
        assert r.returncode == 1 and "格式" in json.loads(r.stdout)["error"], bad
        assert cq in [q["id"] for q in proj.state()["queue"]]
        assert proj.state()["tickets"][t1]["status"] == "draft"


def test_propose_validates_metric_evidence_walls_strings(proj):
    cases = [("metric", None), ("metric", {"now": "1"}), ("evidence", "测试证据甲"),
             ("walls", "测试墙甲"), ("walls", []), ("title", " ")]
    for field, bad in cases:
        d = load()
        if bad is None:
            del d["problems"][1][field]
        else:
            d["problems"][1][field] = bad
        r = propose(proj, d, check=False)
        assert r.returncode == 1 and not json.loads(r.stdout)["ok"], (field, bad)
    s = proj.state() if (proj.home / "state.json").exists() else {}
    assert not s.get("proposals") and not s.get("queue")


def test_propose_skips_key_already_pending(proj):
    propose(proj, load())
    out = json.loads(propose(proj, load()).stdout)
    assert out["proposals"] == [] and out["skipped"] == ["k1", "k2"]
    assert len([q for q in proj.state()["queue"] if q["kind"] == "verify_problem"]) == 2


# ---- Task 4：上线 / 判定 / 尝试循环 ------------------------------------------

def _biz(proj, status="postlaunch", attempts=None, walls=2, streak=0, **extra):
    wt = proj.tmp / "wt"; wt.mkdir(exist_ok=True)
    t = {"kind": "business", "authorized": True, "title": "测试业务票", "issue": 51, "card_confirmed": True,
         "attempts": attempts or [], "no_use_streak": streak,
         "walls": [{"id": f"W{i}", "text": f"墙{i}", "status": "open", "source": "analyst", "at": "2026-11-01T00:00:00Z"}
                   for i in range(1, walls + 1)],
         "worker": {"session_id": "sid-biz", "worktree": str(wt), "runs": 1}, **extra}
    seed(proj, T1=(status, ["her-reply-prompt"], t))


def _rel(proj, *extra, check=True):
    return proj.run("released", "--tickets", "T1", "--what", "测试上线", "--metrics", "m1", *extra, check=check)


def test_released_business_requires_rollback(proj):
    _biz(proj, status="await_prod")
    r = _rel(proj, check=False)
    assert r.returncode == 1 and "--rollback" in json.loads(r.stdout)["error"]
    assert proj.state()["tickets"]["T1"]["attempts"] == []
    out = json.loads(_rel(proj, "--rollback", "关掉开关 x").stdout)
    t = proj.state()["tickets"]["T1"]
    assert out["ok"] and t["status"] == "postlaunch"
    assert t["attempts"][0]["n"] == 1 and t["attempts"][0]["rollback"] == "关掉开关 x"
    assert not t.get("postlaunch")                       # 不排 T+1/T+3/T+7


def test_released_refuses_second_inflight(proj):
    _biz(proj, status="await_prod", attempts=[{"n": 1, "direction": "d", "at": "x", "rollback": "r", "verdict": None}])
    r = _rel(proj, "--rollback", "r2", check=False)
    assert r.returncode == 1 and "上一次尝试还没判" in json.loads(r.stdout)["error"]
    _biz(proj, status="await_prod", attempts=[{"n": 1, "direction": "d", "at": "x", "rollback": "r",
                                               "verdict": "pending_sample"}])
    assert _rel(proj, "--rollback", "r2", check=False).returncode == 1


def test_released_inflight_ignores_historical(proj):
    _biz(proj, status="await_prod", attempts=[{"n": 1, "direction": "d", "at": "x", "rollback": "", "verdict": None,
                                               "historical": True}])
    assert _rel(proj, "--rollback", "r2").returncode == 0


def _verdict(proj, v="useless", n=1, *extra, check=True):
    return proj.run("verdict", "T1", "--attempt", str(n), "--verdict", v, "--touched", "10", "--hit-wall", "5",
                    "--continued", "2", "--note", "合成备注", *extra, check=check)


_A1 = [{"n": 1, "direction": "d", "at": "2026-11-01T00:00:00Z", "rollback": "关开关 x", "verdict": None}]


def test_verdict_useless_requeues_even_with_worktree(proj):
    """9b（concern 4 裁决）：新方向一律重新排队，worktree 还在也不在原 worker 上 resume。"""
    _biz(proj, attempts=_A1)
    out = json.loads(_verdict(proj).stdout)
    assert "change_direction" in out["actions"]
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "queued" and t["card_confirmed"] is False and proj.claude() == []
    inbox = json.loads(proj.run("inbox", "--ticket", "T1").stdout)
    assert "换方向" in json.dumps(inbox, ensure_ascii=False)


def test_verdict_useless_third_time_returns_to_user(proj):
    _biz(proj, attempts=_A1, streak=2)
    _verdict(proj)
    s = proj.state()
    assert [q["kind"] for q in s["queue"]] == ["return"]
    assert s["tickets"]["T1"]["status"] == "postlaunch" and s["tickets"]["T1"]["waiting_for"] == "连续 3 次没用，退回用户定夺"
    assert proj.claude() == []
    assert proj.out("tick")["return_to_user"] == [{"ticket": "T1", "attempt": 1}]


def test_verdict_harmful_queues_harm_and_wakes(proj):
    _biz(proj, attempts=_A1)
    _verdict(proj, "harmful")
    s = proj.state()
    assert [q["kind"] for q in s["queue"]] == ["harm"] and proj.claude() == []
    res = proj.out("tick")
    assert res["rollback_needed"] == [{"ticket": "T1", "rollback": "关开关 x"}]
    assert "rollback_needed" not in proj.out("tick")        # 弹出后不再重复


def test_verdict_no_entry_wakes(proj):
    _biz(proj, attempts=_A1)
    _verdict(proj, "no_entry")
    assert proj.out("tick")["no_entry"] == [{"ticket": "T1", "attempt": 1}]


def test_verdict_useful_next_wall_requeues(proj):
    _biz(proj, attempts=_A1)
    out = json.loads(_verdict(proj, "useful").stdout)
    assert out["actions"] == ["next_wall"]
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "queued" and [w["status"] for w in t["walls"]] == ["broken", "open"]
    assert proj.claude() == []


def test_verdict_next_wall_with_no_open_walls_does_not_resume(proj):
    _biz(proj, attempts=_A1, walls=1)
    _verdict(proj, "useful")
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "postlaunch" and t["waiting_for"] == "墙已全部拆完，等分析 agent 提议结束"
    assert proj.claude() == []


def test_verdict_pending_sample_waits(proj):
    _biz(proj, attempts=_A1)
    assert json.loads(_verdict(proj, "pending_sample").stdout)["actions"] == ["wait"]
    assert proj.state()["tickets"]["T1"]["status"] == "postlaunch" and proj.claude() == []


def _daily(proj, data):
    return json.loads(proj.run("apply-daily", "--from", put(proj, data, "d.json")).stdout)


def test_apply_daily_attaches_wall_and_skips_leak(proj):
    _biz(proj, attempts=_A1)
    d = {"verdicts": [{"ticket": "T1", "attempt": 1, "verdict": "pending_sample", "touched": 3, "hit_wall": 1,
                       "continued": 0, "note": "样本不足"},
                      {"ticket": "T1", "attempt": 1, "verdict": "pending_sample", "touched": 3, "hit_wall": 1,
                       "continued": 0, "note": "见用户 5eed0001"},
                      {"ticket": "T9", "attempt": 1, "verdict": "useful", "touched": 1, "hit_wall": 1, "continued": 1,
                       "note": "x"}],
         "walls": [{"ticket": "T1", "text": "新墙：入口太深"}, {"ticket": "T1", "text": "用户 5eed0001 说的"}]}
    out = _daily(proj, d)
    t = proj.state()["tickets"]["T1"]
    assert [w["text"] for w in t["walls"]][-1] == "新墙：入口太深" and len(t["walls"]) == 3
    assert t["walls"][-1]["source"] == "daily"
    assert t["attempts"][0]["note"] == "样本不足"
    assert len(out["errors"]) == 3 and len(out["applied"]) == 2
    assert "5eed0001" in json.dumps(out["errors"], ensure_ascii=False) or "泄漏" in json.dumps(out["errors"], ensure_ascii=False)


def test_legacy_released_unchanged(proj):
    wt = proj.tmp / "wt"; wt.mkdir(exist_ok=True)
    seed(proj, T1=("await_prod", ["her-reply-prompt"], {"issue": 51, "card_confirmed": True,
                                                        "worker": {"session_id": "s", "worktree": str(wt), "runs": 1}}))
    proj.out("released", "--tickets", "T1", "--what", "x", "--metrics", "m")
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "postlaunch" and len(t["postlaunch"]["checks"]) == 3


def test_verdict_useless_without_worktree_queues_and_tells(proj):
    _biz(proj, attempts=_A1)
    st = proj.state(); st["tickets"]["T1"]["worker"]["worktree"] = None
    (proj.home / "state.json").write_text(json.dumps(st))
    _verdict(proj)
    t = proj.state()["tickets"]["T1"]
    assert t["attempts"][0]["verdict"] == "useless" and t["status"] == "queued" and proj.claude() == []
    inbox = json.loads(proj.run("inbox", "--ticket", "T1").stdout)
    assert "换方向" in json.dumps(inbox, ensure_ascii=False)


def test_verdict_resume_failure_keeps_verdict_and_pushes_stuck(proj):
    _biz(proj, attempts=_A1)
    st = proj.state(); st["tickets"]["T1"]["worker"]["worktree"] = str(proj.tmp / "wt")
    st["tickets"]["T1"]["dispatch"] = {"phase": "spawning", "unverified": "测试"}
    (proj.home / "state.json").write_text(json.dumps(st))
    _verdict(proj)
    s = proj.state()
    assert s["tickets"]["T1"]["attempts"][0]["verdict"] == "useless"
    assert "stuck" in [q["kind"] for q in s["queue"]]


def test_verdict_refuses_rejudge(proj):
    _biz(proj, attempts=[{**_A1[0], "verdict": "useful"}])
    r = _verdict(proj, "harmful", check=False)
    assert r.returncode == 1 and "不许重判" in json.loads(r.stdout)["error"]
    out = _daily(proj, {"verdicts": [{"ticket": "T1", "attempt": 1, "verdict": "harmful", "touched": 1, "hit_wall": 1,
                                      "continued": 1, "note": "x"}]})
    assert len(out["errors"]) == 1 and proj.state()["tickets"]["T1"]["attempts"][0]["verdict"] == "useful"


def test_apply_daily_bad_file_and_bad_ints(proj):
    _biz(proj, attempts=_A1)
    r = proj.run("apply-daily", "--from", str(proj.tmp / "nope.json"), check=False)
    assert r.returncode == 1 and json.loads(r.stdout)["ok"] is False
    (proj.tmp / "l.json").write_text("[1]")
    assert proj.run("apply-daily", "--from", str(proj.tmp / "l.json"), check=False).returncode == 1
    out = _daily(proj, {"verdicts": [{"ticket": "T1", "attempt": 1, "verdict": "useless", "touched": "x", "hit_wall": 1,
                                      "continued": 1, "note": "n"}]})
    assert len(out["errors"]) == 1 and proj.state()["tickets"]["T1"]["attempts"][0]["verdict"] is None


def test_released_business_rejected_writes_no_release_file(proj):
    _biz(proj, status="await_prod")
    _rel(proj, check=False)
    assert not (proj.tmp / "daily" / "state" / "releases.json").exists()


_CARD = "## 方向卡\n**3. 方案方向**\n- 改一处\n**4. 为什么不选另一种做法**\ny\n"


def _card(proj):
    c = proj.home / "cards" / "T1.md"; c.parent.mkdir(exist_ok=True); c.write_text(_CARD)
    return c


def test_business_card_auto_confirmed(proj):
    _biz(proj, status="kickoff", card_confirmed=False)
    c = _card(proj)
    out = json.loads(proj.run("submit-card", "T1", "--card", str(c), "--rounds", "1").stdout)
    s = proj.state(); t = s["tickets"]["T1"]
    assert out["ok"] and not [q for q in s["queue"] if q["kind"] == "card"]
    assert t["card_confirmed"] is True and t["status"] == "executing"
    assert "票级授权" in (proj.home / "decisions.md").read_text() and str(c) in (proj.home / "decisions.md").read_text()
    wait_until(lambda: proj.claude())                          # worker 被叫醒
    prompt = proj.claude()[0][proj.claude()[0].index("-p") + 1]
    assert "票级授权" in prompt and "用户原话（逐字）" not in prompt and "不是用户原话" in prompt
    assert (proj.home / "decisions.md").read_text().count("票级授权") == 1


def test_legacy_card_still_to_user(proj):
    seed(proj, T1=("kickoff", ["her-reply-prompt"], {"worker": {"session_id": "s", "worktree": str(proj.tmp), "runs": 1}}))
    c = _card(proj)
    proj.run("submit-card", "T1", "--card", str(c))
    s = proj.state()
    assert [q["kind"] for q in s["queue"]] == ["card"] and s["tickets"]["T1"]["status"] == "await_card"
    assert not s["tickets"]["T1"].get("card_confirmed")


def test_prod_batch_separates_router_ships(proj):
    """终审 #6：老票 T1 待放行时，业务票 T2 不能被 router 自己带上 prod（合 main 会把 T1 一起带上去）：
    router_ships 为空，整批交用户，prod 项列出每张票的状态。"""
    seed(proj, T1=("await_prod", [], {"title": "老票"}),
         T2=("await_prod", [], {"kind": "business", "authorized": True, "title": "业务票"}))
    out = json.loads(proj.run("prod-batch").stdout)
    s = proj.state()
    assert out["tickets"] == ["T1"] and out["router_ships"] == []
    (summary,) = [q["summary"] for q in s["queue"] if q["kind"] == "prod"]
    assert summary.startswith("上 prod（整批") and "T1 老票·待放行" in summary and "T2 业务票·可上" in summary


def test_prod_batch_only_business_makes_no_item(proj):
    push_main(proj.root)                                                     # staging 不领先 main：没有对不上的提交
    seed(proj, T2=("await_prod", [], {"kind": "business", "authorized": True}))
    out = json.loads(proj.run("prod-batch").stdout)
    assert out["queue"] is None and out["tickets"] == [] and out["router_ships"] == ["T2"]
    assert not proj.state()["queue"]


def test_requeued_business_ticket_needs_new_card(proj):
    _biz(proj, attempts=_A1)
    st = proj.state(); st["tickets"]["T1"]["worker"]["worktree"] = None
    (proj.home / "state.json").write_text(json.dumps(st))
    _verdict(proj)
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "queued" and t["card_confirmed"] is False       # 新一轮必须出新方向卡
    assert "card_ready_at" not in t
    assert t["worker"]["session_id"] is None                              # 9b round 2：重排队清掉旧会话
    # 模拟调度器重派后 worker 交卡：自动确认（重派时 prepare_run 给新会话）
    st = proj.state(); st["tickets"]["T1"]["status"] = "kickoff"
    st["tickets"]["T1"]["worker"].update(worktree=str(proj.tmp / "wt"), session_id="sid-new")
    (proj.home / "state.json").write_text(json.dumps(st))
    proj.run("submit-card", "T1", "--card", str(_card(proj)))
    t = proj.state()["tickets"]["T1"]
    assert t["card_confirmed"] is True and t["status"] == "executing"


def test_unauthorized_business_card_and_prod_batch_legacy(proj):
    seed(proj, T1=("kickoff", ["her-reply-prompt"], {"kind": "business", "authorized": False,
                                                    "worker": {"session_id": "s", "worktree": str(proj.tmp), "runs": 1}}),
         T2=("await_prod", [], {"kind": "business", "authorized": False}))
    proj.run("submit-card", "T1", "--card", str(_card(proj)))
    s = proj.state()
    assert [q["kind"] for q in s["queue"]] == ["card"] and s["tickets"]["T1"]["status"] == "await_card"
    out = json.loads(proj.run("prod-batch").stdout)
    assert out["tickets"] == ["T2"] and out["router_ships"] == []
    q = next(x for x in proj.out("status", "--json")["queue"] if x["kind"] == "prod")
    assert q["blocks"] == 1


def test_prod_item_blocks_excludes_router_owned(proj):
    seed(proj, T1=("await_prod", [], {}), T2=("await_prod", [], {"kind": "business", "authorized": True}))
    proj.run("prod-batch")
    q = next(x for x in proj.out("status", "--json")["queue"] if x["kind"] == "prod")
    assert q["blocks"] == 1


# ---- Task 7：维护票 + impact -------------------------------------------------

import argparse
import router
import store as _store
from datetime import datetime, timezone


class _Clock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now


def _env(proj, now=datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)):
    return router.Env(proj.root, _Clock(now))


def _maint(proj, title="甲", problem="p", surfaces="llm-config", *extra, check=True):
    return proj.run("maint", "add", "--title", title, "--problem", problem, "--surfaces", surfaces, *extra, check=check)


def _maints(proj):
    return [t for t in proj.state()["tickets"].values() if t.get("kind") == "maint"]


def test_maint_add_appends_weekly(proj):
    a = json.loads(_maint(proj, "甲", "p1", "llm-config").stdout)["ticket"]
    b = json.loads(_maint(proj, "乙", "p2", "her-reply-prompt").stdout)["ticket"]
    assert a == b and len(_maints(proj)) == 1
    t = _maints(proj)[0]
    assert [x["text"] for x in t["tasks"]] == ["甲：p1", "乙：p2"] and all(x["done"] is False for x in t["tasks"])
    assert t["surfaces"] == ["her-reply-prompt", "llm-config"] and t["weight"] == "heavy"
    assert t["title"].startswith("维护 20") and "-W" in t["title"]
    assert t["issue"] and t["start_ok"] and t["status"] in ("queued", "kickoff")      # 自动 start


def test_maint_unblocks_standalone(proj):
    seed(proj, T3=("draft", []))
    a = json.loads(_maint(proj, "甲", "p", "llm-config").stdout)["ticket"]
    b = json.loads(_maint(proj, "解锁", "q", "llm-config", "--unblocks", "T3").stdout)["ticket"]
    assert a != b and len(_maints(proj)) == 2
    t = proj.state()["tickets"][b]
    assert t["unblocks"] == "T3" and t["title"] == "维护（解锁 T3）：解锁" and t["status"] in ("queued", "kickoff")


def test_maint_rejects_bad_input(proj):
    for args in (("", "p", "llm-config"), ("甲", "  ", "llm-config"), ("甲", "p", "nope")):
        r = _maint(proj, *args, check=False)
        assert r.returncode == 1 and not json.loads(r.stdout)["ok"]
    r = _maint(proj, "甲", "p", "llm-config", "--unblocks", "T99", check=False)
    assert r.returncode == 1 and "T99" in json.loads(r.stdout)["error"]
    assert not (proj.home / "state.json").exists() or not _maints(proj)


def test_maint_new_week_new_ticket(proj, monkeypatch):
    with _env(proj, datetime(2026, 11, 12, tzinfo=timezone.utc)).state() as st:
        a = router.maint_add(_env(proj, datetime(2026, 11, 12, tzinfo=timezone.utc)), st, {"title": "甲", "problem": "p", "surfaces": []})
        a2 = router.maint_add(_env(proj, datetime(2026, 11, 13, tzinfo=timezone.utc)), st, {"title": "乙", "problem": "p", "surfaces": []})
        b = router.maint_add(_env(proj, datetime(2026, 11, 17, tzinfo=timezone.utc)), st, {"title": "丙", "problem": "p", "surfaces": []})
    assert a == a2 and b != a
    s = proj.state()
    assert s["tickets"][a]["title"] == "维护 2026-W46" and s["tickets"][b]["title"] == "维护 2026-W47"


def test_maint_running_ticket_gets_inbox_note(proj, monkeypatch):
    import launcher
    env = _env(proj)
    with env.state() as st:
        a = router.maint_add(env, st, {"title": "甲", "problem": "p", "surfaces": []})
        assert st["tickets"][a]["status"] == "draft"                  # maint_add 永不 start
        _store.set_status(st, a, "executing", env.now())
    for alive in (True, False):                                       # worker 死了也记：续跑时读 inbox
        monkeypatch.setattr(launcher, "worker_alive", lambda ctx, w, v=alive: v)
        with env.state() as st:
            b = router.maint_add(env, st, {"title": f"乙{alive}", "problem": "q", "surfaces": []})
        assert a == b
    lines = [json.loads(l) for l in (proj.home / "inbox" / f"{a}.jsonl").read_text().splitlines()]
    assert len(lines) == 2 and "乙True：q" in lines[0]["summary"] and len(proj.state()["tickets"][a]["tasks"]) == 3


def test_maint_add_idempotent(proj, monkeypatch):
    seed(proj, T3=("draft", []))
    env = _env(proj)
    item = {"title": "甲", "problem": "p", "surfaces": ["llm-config"]}
    with env.state() as st:
        a = router.maint_add(env, st, item)
        assert router.maint_add(env, st, item) == a and len(st["tickets"][a]["tasks"]) == 1
        u = router.maint_add(env, st, {**item, "unblocks": "T3"})
        assert router.maint_add(env, st, {**item, "unblocks": "T3"}) == u and u != a
    assert len([t for t in proj.state()["tickets"].values() if t.get("kind") == "maint"]) == 2


def _fail_start(monkeypatch):
    def boom(env, st, sel):
        raise router.Fail("模拟 start 失败")
    monkeypatch.setattr(router, "_start_locked", boom)


def test_cli_propose_start_failure_keeps_proposals_no_dupes(proj, monkeypatch):
    # 子进程里没法 monkeypatch：用 gh 报错模拟 start 失败（建 issue 失败）
    canned = proj.tmp / "canned.json"
    canned.write_text(json.dumps({"issue create": {"rc": 1, "err": "boom"}, "api": {"rc": 1, "err": "boom"}}))
    d = load()
    d["maint"] = [{"title": "甲", "problem": "p", "surfaces": ["llm-config"]}]
    r = proj.run("propose", "--from", put(proj, d), env={"GH_CANNED": str(canned)}, check=False)
    assert r.stdout, r.stderr
    out = json.loads(r.stdout)
    assert r.returncode == 1 and "DRAFT" in out["error"] and "重试" in out["error"]
    s = proj.state()
    assert len(s["proposals"]) == 2 and len([q for q in s["queue"] if q["kind"] == "verify_problem"]) == 2
    (mt,) = _maints(proj)
    assert mt["status"] == "draft" and len(mt["tasks"]) == 1
    # 重跑同一份：提议被「待判」去重跳过，维护条目不重复追加
    r = proj.run("propose", "--from", put(proj, d), env={"GH_CANNED": str(canned)}, check=False)
    s = proj.state()
    assert len(s["proposals"]) == 2 and len(_maints(proj)) == 1 and len(_maints(proj)[0]["tasks"]) == 1
    # gh 好了再跑：同一张票被 start
    out = json.loads(propose(proj, d).stdout)
    (mt,) = _maints(proj)
    assert mt["issue"] and mt["status"] in ("queued", "kickoff") and len(mt["tasks"]) == 1


def test_maint_add_cli_start_failure_saved_as_draft(proj):
    canned = proj.tmp / "canned.json"
    canned.write_text(json.dumps({"issue create": {"rc": 1, "err": "boom"}, "api": {"rc": 1, "err": "boom"}}))
    r = proj.run("maint", "add", "--title", "甲", "--problem", "p", "--surfaces", "llm-config",
                 env={"GH_CANNED": str(canned)}, check=False)
    assert r.returncode == 1 and "DRAFT" in json.loads(r.stdout)["error"]
    (mt,) = _maints(proj)
    assert mt["status"] == "draft"
    _maint(proj, "甲", "p", "llm-config")
    (mt,) = _maints(proj)
    assert len(mt["tasks"]) == 1 and mt["issue"]


def test_maint_router_only(proj):
    for args in (("maint", "add", "--title", "x", "--problem", "p", "--surfaces", "llm-config"),
                 ("impact", "T1", "--size", "0.1", "--gap", "0.1", "--confidence", "low")):
        r = proj.run(*args, env={"ROUTER_TICKET": "T1"}, check=False)
        assert r.returncode == 2


def test_propose_maint_creates_tickets_and_validates(proj):
    d = load()
    d["maint"] = [{"title": "甲", "problem": "p", "surfaces": ["llm-config"]},
                  {"title": "乙", "problem": "q", "surfaces": ["her-reply-prompt"]}]
    out = json.loads(propose(proj, d).stdout)
    assert len(out["maint"]) == 1 and len(_maints(proj)) == 1 and len(_maints(proj)[0]["tasks"]) == 2
    for bad in ([{"title": "", "problem": "p", "surfaces": []}],
                [{"title": "x", "problem": "p", "surfaces": ["nope"]}],
                [{"title": "x", "problem": "p", "surfaces": "llm-config"}],
                [{"title": "x", "problem": "p", "surfaces": [], "unblocks": "T99"}]):
        n = len(proj.state()["proposals"])
        r = propose(proj, load(maint=bad), check=False)
        assert r.returncode == 1 and not json.loads(r.stdout)["ok"], bad
        assert len(proj.state()["proposals"]) == n


def test_propose_maint_leak_refused(proj):
    (proj.runs / "2026-10-26" / "users.json").write_text(json.dumps({"deadbeef": {}}))
    r = propose(proj, load(maint=[{"title": "x", "problem": "deadbeef 说", "surfaces": []}]), check=False)
    assert r.returncode == 1 and "deadbeef" in r.stdout
    assert not (proj.home / "state.json").exists() or (not _maints(proj) and not proj.state().get("proposals"))


def _biz_state(proj):
    propose(proj, load())
    return json.loads(_answer(proj, _first_q(proj), "真，先放着").stdout)["ticket"]


def test_impact_sets_and_records_decision(proj):
    tid = _biz_state(proj)
    out = proj.out("impact", tid, "--size", "0.6", "--gap", "0.5", "--confidence", "high")
    assert out["ok"] and proj.state()["tickets"][tid]["impact"] == {"size": 0.6, "gap": 0.5, "confidence": "high"}
    assert out["decision"] in (proj.home / "decisions.md").read_text()


def test_impact_rejects_legacy_and_bad_values(proj):
    tid = _biz_state(proj)
    seed_state = proj.state()
    seed_state["tickets"]["T9"] = {**seed_state["tickets"][tid], "id": "T9", "kind": None}
    (proj.home / "state.json").write_text(json.dumps(seed_state))
    r = proj.run("impact", "T9", "--size", "0.5", "--gap", "0.5", "--confidence", "mid", check=False)
    assert r.returncode == 1 and "不是业务票" in json.loads(r.stdout)["error"]
    for sz, gp in (("1.5", "0.5"), ("0.5", "-0.1")):
        r = proj.run("impact", tid, "--size", sz, "--gap", gp, "--confidence", "mid", check=False)
        assert r.returncode == 1 and "0~1" in json.loads(r.stdout)["error"]
    r = proj.run("impact", tid, "--size", "0.5", "--gap", "0.5", "--confidence", "huge", check=False)
    assert r.returncode == 2
    assert proj.state()["tickets"][tid]["impact"] != {"size": 1.5, "gap": 0.5, "confidence": "mid"}


def test_status_json_adds_kind_and_business_fields(tmp_path):
    import store
    from conftest import make_state
    import router
    st = make_state(T1=("executing", ["a"]))
    b = store._ticket_defaults()
    b.update(id="T2", kind="business", walls=[{"id": "W1", "text": "x", "status": "open"},
                                              {"id": "W2", "text": "y", "status": "broken"}],
             attempts=[{"n": 1, "verdict": "useful", "at": "t"}, {"n": 2, "verdict": "legacy", "legacy_verdict": "flat", "historical": True,
                                                                                    "shipped_at": "2026-10-20T00:00:00Z"}])
    m = store._ticket_defaults()
    m.update(id="T3", kind="maint", tasks=[{"text": "a", "done": True}, {"text": "b", "done": False}])
    st["tickets"].update(T2=b, T3=m)
    out = router._status_tickets(st)
    assert out["T1"]["kind"] == "legacy" and "walls_open" not in out["T1"]
    assert out["T2"]["kind"] == "business" and out["T2"]["walls_open"] == 1
    assert out["T2"]["last_verdict"] == "useful"          # 9b round 2：当前尝试 = 最后一条非历史的；没有才看历史
    b["attempts"] = b["attempts"][1:]
    assert router._status_tickets(st)["T2"]["last_verdict"] == "flat"
    assert out["T3"]["kind"] == "maint" and out["T3"]["tasks_open"] == 1
    assert "kind" not in st["tickets"]["T1"]


# ---- Task 9b：换方向（redirect）/ router 代答（answer --as-router）/ 关票（close）/ 并旧票（absorb） ----

def _push_q(proj, ticket, kind, summary="合成项"):
    st = proj.state()
    st["seq"]["queue"] += 1
    qid = f"Q{st['seq']['queue']}"
    st["queue"].append({"id": qid, "ticket": ticket, "kind": kind, "summary": summary, "created_at": "x",
                        "notified_at": "x"})
    (proj.home / "state.json").write_text(json.dumps(st, ensure_ascii=False))
    return qid


def _inbox_text(proj, tid="T1"):
    return json.dumps(json.loads(proj.run("inbox", "--ticket", tid).stdout), ensure_ascii=False)


def test_redirect_after_harmful_requeues_as_router(proj):
    _biz(proj, attempts=_A1)
    _verdict(proj, "harmful")
    out = proj.out("redirect", "T1", "换成测试方向乙", "--as-router")
    s = proj.state(); t = s["tickets"]["T1"]
    assert out["ok"] and t["status"] == "queued" and t["card_confirmed"] is False
    assert not [q for q in s["queue"] if q["kind"] == "harm"]                  # harm 项清掉
    assert proj.claude() == []                                                  # 不 resume：交给调度器重新派
    inbox = _inbox_text(proj)
    assert "router 决定" in inbox and "换成测试方向乙" in inbox and "用户原话" not in inbox
    dec = (proj.home / "decisions.md").read_text()
    assert "换方向" in dec and "换成测试方向乙" in dec
    assert not (proj.home / "answers.md").exists() or "换成测试方向乙" not in (proj.home / "answers.md").read_text()


def test_redirect_after_return_as_user_resets_streak(proj):
    _biz(proj, attempts=_A1, streak=2)
    _verdict(proj)                                                              # 第 3 次没用 ⇒ return
    assert [q["kind"] for q in proj.state()["queue"]] == ["return"]
    proj.out("redirect", "T1", "测试用户给的方向", "--as-user")
    s = proj.state(); t = s["tickets"]["T1"]
    assert t["status"] == "queued" and t["no_use_streak"] == 0 and s["queue"] == []
    inbox = _inbox_text(proj)                                                   # 读一次就标已读：只读一次
    assert "用户原话（逐字）" in inbox and "测试用户给的方向" in inbox
    assert "测试用户给的方向" in (proj.home / "answers.md").read_text()        # 用户原话照常存档


def test_redirect_refusals(proj):
    seed(proj, T1=("postlaunch", [], {"title": "老票"}))
    r = proj.run("redirect", "T1", "x", "--as-router", check=False)
    assert r.returncode == 1 and "业务票" in json.loads(r.stdout)["error"]
    _biz(proj, status="executing", attempts=_A1)
    r = proj.run("redirect", "T1", "x", "--as-router", check=False)
    assert r.returncode == 1 and "postlaunch" in json.loads(r.stdout)["error"]
    for v in (None, "pending_sample"):                                          # 终审 #3：只拒在途（还在等判定）
        _biz(proj, attempts=[{**_A1[0], "verdict": v}])
        r = proj.run("redirect", "T1", "x", "--as-router", check=False)
        assert r.returncode == 1, v
        assert proj.state()["tickets"]["T1"]["status"] == "postlaunch"
    _biz(proj, attempts=[{**_A1[0], "verdict": "harmful"}])
    assert proj.run("redirect", "T1", "x", check=False).returncode == 2         # 来源必须写明：--as-router / --as-user
    assert proj.run("redirect", "T1", "  ", "--as-router", check=False).returncode == 1
    assert proj.run("redirect", "T1", "x", "--as-router", env={"ROUTER_TICKET": "T1"}, check=False).returncode == 2


def test_redirect_refused_while_worker_alive(proj):
    import subprocess, sys
    _biz(proj, attempts=[{**_A1[0], "verdict": "harmful"}])
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        st = proj.state(); st["tickets"]["T1"]["worker"]["pid"] = p.pid
        (proj.home / "state.json").write_text(json.dumps(st))
        r = proj.run("redirect", "T1", "x", "--as-router", check=False)
        assert r.returncode == 1 and "stop" in json.loads(r.stdout)["error"]
        assert proj.state()["tickets"]["T1"]["status"] == "postlaunch"
    finally:
        p.kill(); p.wait()


def test_answer_as_router_labels_router_decision(proj):
    _biz(proj, status="paused")
    qid = _push_q(proj, "T1", "direction", "T1：要不要换做法")
    out = proj.out("answer", qid, "试测试方向丙", "--as-router")
    assert out["resumed"] == "T1"
    wait_until(lambda: proj.claude())
    prompt = proj.claude()[0][proj.claude()[0].index("-p") + 1]
    assert "router 决定" in prompt and "试测试方向丙" in prompt and "用户原话（逐字）" not in prompt
    line = (proj.home / "answers.md").read_text().strip().splitlines()[-1]
    assert "router 决定：试测试方向丙" in line


def test_answer_as_router_refusals(proj):
    seed(proj, T1=("paused", [], {"title": "老票", "worker": {"session_id": "s", "worktree": str(proj.tmp), "runs": 1}}))
    qid = _push_q(proj, "T1", "direction")
    r = proj.run("answer", qid, "x", "--as-router", check=False)
    assert r.returncode == 1 and "业务票" in json.loads(r.stdout)["error"]
    assert qid in [q["id"] for q in proj.state()["queue"]]                      # 拒了：项还在
    _biz(proj, status="paused")
    for kind in ("spend", "harm", "return"):                                    # 花钱 / 撤不回 / 发商店包、退回用户的：只能用户回
        qid = _push_q(proj, "T1", kind)
        r = proj.run("answer", qid, "x", "--as-router", check=False)
        assert r.returncode == 1, kind
    propose(proj, load(maint=[]))
    r = proj.run("answer", _first_q(proj), "真", "--as-router", check=False)    # 判真只归用户
    assert r.returncode == 1
    assert proj.claude() == []


def test_close_command_closes_business_and_clears_items(proj):
    t1 = _biz_closeable(proj)
    _push_q(proj, t1, "harm")
    out = proj.out("close", t1)
    s = proj.state()
    assert out["ok"] and s["tickets"][t1]["status"] == "done"
    assert s["tickets"][t1]["closeout"]["worktree"] == "pending"
    assert not [q for q in s["queue"] if q["ticket"] == t1]
    assert "关闭业务票" in (proj.home / "decisions.md").read_text()


def test_close_command_refusals(proj):
    seed(proj, T1=("postlaunch", [], {"title": "老票"}))
    r = proj.run("close", "T1", check=False)
    assert r.returncode == 1 and "业务票" in json.loads(r.stdout)["error"]
    import subprocess, sys
    t1 = _biz_closeable(proj)
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        s = proj.state(); s["tickets"][t1]["worker"]["pid"] = p.pid
        (proj.home / "state.json").write_text(json.dumps(s))
        r = proj.run("close", t1, check=False)
        assert r.returncode == 1 and "stop" in json.loads(r.stdout)["error"]
        assert proj.state()["tickets"][t1]["status"] == "draft"
    finally:
        p.kill(); p.wait()
    proj.out("close", t1)
    r = proj.run("close", t1, check=False)                                      # 已关的不再关
    assert r.returncode == 1


_HIST5 = {"n": 1, "direction": "老票五", "shipped_at": "2026-11-01T00:00:00Z", "verdict": "legacy",
          "legacy_verdict": "没用", "historical": True}


def test_absorb_into_existing_business(proj):
    _biz(proj, status="executing")
    st = proj.state()
    for tid, status, extra in (("T5", "postlaunch", {"title": "老票五", "postlaunch": {
                                   "t0": "2026-11-01T00:00:00Z", "checks": [],
                                   "results": [{"check": "T+1", "verdict": "没用"}]}}),
                               ("T6", "executing", {"title": "老票六"})):
        from conftest import make_state
        st["tickets"][tid] = make_state(**{tid: (status, [], extra)})["tickets"][tid]
    (proj.home / "state.json").write_text(json.dumps(st, ensure_ascii=False))
    out = proj.out("absorb", "T1", "T5", "T6")
    s = proj.state(); t1 = s["tickets"]["T1"]
    assert out["ok"] and s["tickets"]["T5"]["parent"] == "T1" and s["tickets"]["T6"]["parent"] == "T1"
    assert t1["attempts"] == [_HIST5] and t1["absorbs"] == ["T5", "T6"]       # 在做的 T6 只挂 parent
    assert "T5" in (proj.home / "decisions.md").read_text()
    proj.out("absorb", "T1", "T5")                                              # 重复并：不重复记尝试
    assert proj.state()["tickets"]["T1"]["attempts"] == [_HIST5]


def test_absorb_refusals(proj):
    from conftest import make_state
    s = make_state(T1=("postlaunch", [], {"title": "老票"}), T2=("executing", [], {"title": "老票二"}),
                   T3=("draft", [], {"kind": "business", "authorized": True, "title": "业务三", "attempts": [],
                                     "walls": [], "absorbs": []}),
                   T4=("draft", [], {"kind": "business", "authorized": True, "title": "业务四", "attempts": [],
                                     "walls": [], "absorbs": []}),
                   T5=("executing", [], {"title": "老票五", "parent": "T4"}))
    (proj.home / "state.json").write_text(json.dumps(s, ensure_ascii=False))
    for args in (("T1", "T2"),            # 目标不是业务票
                 ("T3", "T4"),            # 并的不是老票
                 ("T3", "T5"),            # 已并进别的业务票
                 ("T3", "T99"),           # 不存在
                 ("T3", "T3")):
        r = proj.run("absorb", *args, check=False)
        assert r.returncode == 1, args
    assert proj.state()["tickets"]["T2"].get("parent") is None
    assert proj.run("absorb", "T3", "T2", env={"ROUTER_TICKET": "T3"}, check=False).returncode == 2


def test_parented_legacy_postlaunch_result_records_parent_attempt(proj):
    from datetime import datetime as dt, timedelta as td, timezone as tz
    t0 = (dt.now(tz.utc) - td(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seed(proj, T1=("await_prod", ["her-reply-prompt"], {"title": "老票一", "issue": 51, "card_confirmed": True,
                                                        "parent": "T2"}),
         T2=("executing", [], {"kind": "business", "authorized": True, "title": "业务二", "attempts": [],
                               "walls": [], "absorbs": ["T1"]}))
    proj.out("released", "--tickets", "T1", "--what", "x", "--metrics", "m", "--t0", t0)
    assert proj.state()["tickets"]["T2"]["attempts"] == []                     # 上线了还没验收：不算
    args = ["postlaunch-result", "T1", "--check", "T+1", "--type", "风险型", "--summary", "合成摘要"]
    proj.out(*args, "--verdict", "看不出来")
    atts = proj.state()["tickets"]["T2"]["attempts"]
    assert atts == [{"n": 1, "direction": "老票一", "shipped_at": t0, "verdict": "legacy",
                     "legacy_verdict": "看不出来", "historical": True}]
    proj.out(*args, "--verdict", "生效")                                       # 同检查点覆盖：更新那一条，不重复记
    atts = proj.state()["tickets"]["T2"]["attempts"]
    assert len(atts) == 1 and atts[0]["legacy_verdict"] == "生效"
    assert proj.state()["tickets"]["T1"]["status"] == "done"


def _seed_migrate(proj, data, **more):
    """老票 T5（验收过）、T6（在做）、业务票 T7；一次跑完、收过的 migrate run，产出 = data。"""
    from conftest import make_state
    st = make_state(T5=("postlaunch", [], {"title": "老票五", "postlaunch": {
                        "t0": "2026-11-01T00:00:00Z", "checks": [], "results": [{"check": "T+1", "verdict": "没用"}]}}),
                    T6=("executing", [], {"title": "老票六"}),
                    T7=("executing", [], {"kind": "business", "authorized": True, "title": "业务七", "attempts": [],
                                          "walls": [], "absorbs": []}), **more)
    out = proj.home / "analyst" / "out" / "migrate-1.json"; out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False))
    st["analyst"] = {"runs": {"migrate-1": {"mode": "migrate", "out": str(out), "ingested": "x"}}}
    (proj.home / "state.json").write_text(json.dumps(st, ensure_ascii=False))


def _mig(**over):
    d = load(mode="migrate", maint=[], attach=[{"ticket": "T7", "absorbs": ["T6"]}])
    d["problems"][0]["absorbs"] = ["T5"]
    d.update(over)
    return d


def test_migrate_apply_after_user_confirms(proj):
    """9b round 2：migrate 产出由 router 交用户确认整份清单后，migrate-apply 一次落：问题建业务票（默认 start），
    absorbs / attach 并老票；先放着的只建票。"""
    _seed_migrate(proj, _mig())
    out = proj.out("migrate-apply", "migrate-1", "--said", "合成：迁移清单可以，k2 先放着", "--hold", "k2")
    s = proj.state()
    new = {s["tickets"][t]["title"]: t for t in out["tickets"]}
    t1, t2 = new["测试问题甲"], new["测试问题乙"]
    assert s["tickets"][t1]["kind"] == "business" and s["tickets"][t1]["start_ok"] and s["tickets"][t1]["issue"]
    assert s["tickets"][t2]["status"] == "draft" and s["tickets"][t2]["issue"] is None
    assert s["tickets"]["T5"]["parent"] == t1 and s["tickets"][t1]["attempts"][0]["legacy_verdict"] == "没用"
    assert s["tickets"]["T6"]["parent"] == "T7" and s["tickets"]["T7"]["absorbs"] == ["T6"]
    assert not [q for q in s["queue"] if q["kind"] == "verify_problem"]          # 用户已整份确认：不再逐条判真
    assert s["analyst"]["runs"]["migrate-1"]["applied"]
    assert "合成：迁移清单可以" in (proj.home / "answers.md").read_text()
    assert "迁移清单" in (proj.home / "decisions.md").read_text()
    r = proj.run("migrate-apply", "migrate-1", "--said", "再来一次", check=False)   # 一份清单只落一次
    assert r.returncode == 1 and "已" in json.loads(r.stdout)["error"]


def test_migrate_apply_skip_and_refusals(proj):
    _seed_migrate(proj, _mig())
    r = proj.run("migrate-apply", "migrate-1", "--said", "x", "--skip", "nope", check=False)
    assert r.returncode == 1 and "nope" in json.loads(r.stdout)["error"]
    assert proj.run("migrate-apply", "migrate-9", "--said", "x", check=False).returncode == 1
    assert proj.run("migrate-apply", "migrate-1", check=False).returncode == 2              # 用户原话必填
    assert proj.run("migrate-apply", "migrate-1", "--said", "x", env={"ROUTER_TICKET": "T1"},
                    check=False).returncode == 2
    out = proj.out("migrate-apply", "migrate-1", "--said", "合成：k2 和 T7 那条不要", "--skip", "k2,T7", "--hold", "k1")
    s = proj.state()
    assert [s["tickets"][t]["title"] for t in out["tickets"]] == ["测试问题甲"]
    assert s["tickets"]["T6"].get("parent") is None and s["tickets"]["T5"]["parent"] == out["tickets"][0]


def test_migrate_apply_refuses_bad_list_whole(proj):
    for bad in (_mig(attach=[{"ticket": "T6", "absorbs": ["T5"]}]),                 # 目标不是业务票
                _mig(attach=[{"ticket": "T7", "absorbs": ["T5"]}]),                 # 同一张老票两头并
                _mig(attach=[{"ticket": "T7", "absorbs": ["T99"]}])):
        _seed_migrate(proj, bad)
        r = proj.run("migrate-apply", "migrate-1", "--said", "x", check=False)
        assert r.returncode == 1, bad["attach"]
        s = proj.state()
        assert s["tickets"]["T5"].get("parent") is None and len(s["tickets"]) == 3
    st = proj.state(); st["analyst"]["runs"]["w-1"] = {**st["analyst"]["runs"]["migrate-1"], "mode": "weekly"}
    (proj.home / "state.json").write_text(json.dumps(st, ensure_ascii=False))
    assert proj.run("migrate-apply", "w-1", "--said", "x", check=False).returncode == 1  # 只收 migrate 的 run


def test_propose_refuses_attach(proj):
    _seed_migrate(proj, _mig())
    r = propose(proj, _mig(problems=[]), check=False)
    assert r.returncode == 1 and "migrate-apply" in json.loads(r.stdout)["error"]


# ---- 9b round 2：重排队拿新会话、新分支；当前尝试不被历史尝试盖住；卡住项重排；close 查派单待核实；代答进 inbox 带标 ----

def test_requeued_dispatch_gets_fresh_session_and_branch(proj):
    _biz(proj, attempts=_A1, start_ok=True, pr=77, merge_sha="abc", verifying_at="x")
    _verdict(proj)                                                              # 没用 ⇒ 重新排队
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "queued" and t["worker"]["session_id"] is None and t.get("pr") is None
    assert t["worker"]["branch"] == "router/T1-a2" and not t.get("merge_sha")
    proj.out("tick")
    t = proj.state()["tickets"]["T1"]
    wait_until(lambda: proj.claude())
    argv = proj.claude()[-1]
    sid = argv[argv.index("--session-id") + 1]
    assert t["status"] == "kickoff" and sid != "sid-biz" and sid == t["worker"]["session_id"] and "--resume" not in argv
    assert t["worker"]["worktree"].endswith("router-T1-a2") and t["worker"]["branch"] == "router/T1-a2"
    assert "router/T1-a2" in argv[argv.index("-p") + 1]                          # 派单写的是这次的分支
    import subprocess
    head = subprocess.run(["git", "-C", t["worker"]["worktree"], "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert head == "router/T1-a2"


def test_historical_attempt_inserted_before_live_one(proj):
    from conftest import make_state
    _biz(proj, status="postlaunch", attempts=list(_A1))
    st = proj.state()
    st["tickets"]["T5"] = make_state(T5=("postlaunch", [], {"title": "老票五", "postlaunch": {
        "t0": "2026-11-01T00:00:00Z", "checks": [], "results": [{"check": "T+1", "verdict": "没用"}]}}))["tickets"]["T5"]
    (proj.home / "state.json").write_text(json.dumps(st, ensure_ascii=False))
    proj.out("absorb", "T1", "T5")
    atts = proj.state()["tickets"]["T1"]["attempts"]
    assert [a.get("historical", False) for a in atts] == [True, False] and atts[-1]["n"] == 1
    import analyst, router
    assert "尝试 1「d」" in analyst.state_summary(proj.state(), "daily")
    assert router._status_tickets(proj.state())["T1"]["last_verdict"] is None    # 当前尝试还待判
    r = _rel(proj, "--rollback", "r2", check=False)
    assert r.returncode == 1                                                     # 当前尝试没判：照样拒
    _verdict(proj, "pending_sample")                                             # daily 能判到它
    assert proj.state()["tickets"]["T1"]["attempts"][-1]["verdict"] == "pending_sample"


def test_failed_requeue_stuck_item_requeues_when_answered(proj):
    _biz(proj, attempts=_A1)
    st = proj.state(); st["tickets"]["T1"]["dispatch"] = {"phase": "spawning", "unverified": "测试"}
    (proj.home / "state.json").write_text(json.dumps(st))
    _verdict(proj)
    s = proj.state()
    (q,) = [q for q in s["queue"] if q["kind"] == "stuck"]
    assert q.get("topic") == "requeue" and s["tickets"]["T1"]["status"] == "postlaunch"
    st = proj.state(); st["tickets"]["T1"].pop("dispatch")
    (proj.home / "state.json").write_text(json.dumps(st))
    proj.out("answer", q["id"], "重排", "--as-router")
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "queued" and t["card_confirmed"] is False and proj.claude() == []
    assert "换方向" in _inbox_text(proj)                                         # 判定后的下一步照样带到


def test_redirect_clears_requeue_stuck_item(proj):
    _biz(proj, attempts=_A1)
    st = proj.state(); st["tickets"]["T1"]["dispatch"] = {"phase": "spawning", "unverified": "测试"}
    (proj.home / "state.json").write_text(json.dumps(st))
    _verdict(proj)
    st = proj.state(); st["tickets"]["T1"].pop("dispatch")
    (proj.home / "state.json").write_text(json.dumps(st))
    out = proj.out("redirect", "T1", "测试方向丁", "--as-router")
    s = proj.state()
    assert out["cleared"] and not [q for q in s["queue"] if q["ticket"] == "T1"]


def test_close_refused_while_dispatch_unverified(proj):
    t1 = _biz_closeable(proj, dispatch={"phase": "spawning", "unverified": "测试"})
    r = proj.run("close", t1, check=False)
    assert r.returncode == 1 and "派单待核实" in json.loads(r.stdout)["error"]
    assert proj.state()["tickets"][t1]["status"] == "draft"


def test_answer_as_router_to_live_worker_labels_inbox(proj):
    import subprocess, sys
    _biz(proj, status="executing")
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        st = proj.state(); st["tickets"]["T1"]["worker"]["pid"] = p.pid
        (proj.home / "state.json").write_text(json.dumps(st))
        qid = _push_q(proj, "T1", "direction")
        out = proj.out("answer", qid, "测试决定戊", "--as-router")
        assert out["delivered"] == "T1"
        inbox = _inbox_text(proj)
        assert "router 决定" in inbox and "测试决定戊" in inbox and "用户原话（逐字）" not in inbox
    finally:
        p.kill(); p.wait()


# ---- 9b round 3：重排队不丢东西（留着的 worktree、上一次的 PR）、不碰接管中的票、分支后缀 ---------------------

def _git(proj, *args, cwd=None):
    import subprocess
    return subprocess.run(["git", *args], cwd=cwd or proj.root, check=True, capture_output=True, text=True).stdout


def test_requeue_keeps_dirty_or_kept_worktree(proj):
    """有未提交改动的 / 有意保留的 checkout：重排队不删（不 --force），记进 stale_checkouts，照样排队。"""
    wt = proj.root / ".claude" / "worktrees" / "router-T1"
    _git(proj, "worktree", "add", "-q", "-b", "router/T1", str(wt))
    (wt / "kept.md").write_text("合成：留给人看的未提交改动\n")
    _biz(proj, attempts=_A1)
    st = proj.state(); st["tickets"]["T1"]["worker"].update(worktree=str(wt), branch="router/T1")
    (proj.home / "state.json").write_text(json.dumps(st))
    _verdict(proj)
    t = proj.state()["tickets"]["T1"]
    assert (wt / "kept.md").exists() and t["status"] == "queued"
    (sc,) = t["stale_checkouts"]
    assert sc["worktree"] == str(wt) and "未提交" in sc["why"]
    # 干净、但标了 checkout_kept 的：也不动
    (wt / "kept.md").unlink()
    _biz(proj, attempts=_A1, checkout_kept="worktree：有意保留")
    st = proj.state(); st["tickets"]["T1"]["worker"].update(worktree=str(wt), branch="router/T1")
    (proj.home / "state.json").write_text(json.dumps(st))
    _verdict(proj)
    t = proj.state()["tickets"]["T1"]
    assert wt.is_dir() and t["status"] == "queued" and "有意保留" in t["stale_checkouts"][0]["why"]
    # 干净、没标保留的：照常删（不用 --force）
    _biz(proj, attempts=_A1)
    st = proj.state(); st["tickets"]["T1"]["worker"].update(worktree=str(wt), branch="router/T1")
    (proj.home / "state.json").write_text(json.dumps(st))
    _verdict(proj)
    assert not wt.exists() and not proj.state()["tickets"]["T1"].get("stale_checkouts")


def test_attempt_keeps_its_pr_and_merge_sha_after_requeue(proj):
    _biz(proj, status="await_prod", pr=77, merge_sha="abc")
    st = proj.state(); st["tickets"]["T1"]["worker"]["branch"] = "router/T1"
    (proj.home / "state.json").write_text(json.dumps(st))
    _rel(proj, "--rollback", "关开关 x")
    _verdict(proj)                                                              # 没用 ⇒ 第 2 次尝试开始（重排队）
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "queued" and t.get("pr") is None
    a1 = t["attempts"][0]
    assert a1["pr"] == 77 and a1["merge_sha"] == "abc" and a1["branch"] == "router/T1"


def test_requeue_refuses_takeover(proj):
    _biz(proj, status="takeover", attempts=_A1, takeover_from="postlaunch")
    _verdict(proj)                                                              # 判定照记，排队被拒：票原样留在接管中
    s = proj.state(); t = s["tickets"]["T1"]
    assert t["attempts"][0]["verdict"] == "useless" and t["status"] == "takeover"
    assert t["worker"]["session_id"] == "sid-biz" and t["worker"]["worktree"] == str(proj.tmp / "wt")
    (q,) = [q for q in s["queue"] if q.get("topic") == "requeue"]
    assert "接管" in q["summary"]
    r = proj.run("answer", q["id"], "重排", "--as-router", check=False)
    assert r.returncode == 1 and "接管" in json.loads(r.stdout)["error"]
    assert proj.state()["tickets"]["T1"]["status"] == "takeover"
    assert proj.run("redirect", "T1", "x", "--as-router", check=False).returncode == 1


def test_branch_suffix_counts_live_attempts_not_history(proj):
    """并进来的历史尝试不改变分支后缀：a<k> = 这张票自己的第 k 次尝试；尝试记录里记着它用的分支。"""
    hist = {"n": 2, "direction": "老票", "shipped_at": "x", "verdict": "legacy", "legacy_verdict": "没用",
            "historical": True}
    _biz(proj, attempts=[hist, {**_A1[0]}])
    _verdict(proj)
    assert proj.state()["tickets"]["T1"]["worker"]["branch"] == "router/T1-a2"
