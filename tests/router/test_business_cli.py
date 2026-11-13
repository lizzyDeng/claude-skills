import json

from conftest import FIX
from test_router_cli import proj, seed, wait_until  # noqa: F401  (proj 是 fixture)


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
    assert out["maint_pending"][0]["title"] == "测试维护甲"      # R3：Task 7 才建维护票
    assert not [t for t in s["tickets"].values() if t.get("kind") == "maint"]


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
    propose(proj, load())
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


def test_verdict_useless_resumes_worker(proj):
    _biz(proj, attempts=_A1)
    out = json.loads(_verdict(proj).stdout)
    assert "change_direction" in out["actions"]
    assert proj.state()["tickets"]["T1"]["status"] == "executing"
    wait_until(lambda: proj.claude())
    prompt = proj.claude()[0][proj.claude()[0].index("-p") + 1]
    assert "换方向" in prompt


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


def test_verdict_useful_next_wall_resumes(proj):
    _biz(proj, attempts=_A1)
    out = json.loads(_verdict(proj, "useful").stdout)
    assert out["actions"] == ["next_wall"]
    t = proj.state()["tickets"]["T1"]
    assert t["status"] == "executing" and [w["status"] for w in t["walls"]] == ["broken", "open"]


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
    assert "票级授权" in proj.claude()[0][proj.claude()[0].index("-p") + 1]


def test_legacy_card_still_to_user(proj):
    seed(proj, T1=("kickoff", ["her-reply-prompt"], {"worker": {"session_id": "s", "worktree": str(proj.tmp), "runs": 1}}))
    c = _card(proj)
    proj.run("submit-card", "T1", "--card", str(c))
    s = proj.state()
    assert [q["kind"] for q in s["queue"]] == ["card"] and s["tickets"]["T1"]["status"] == "await_card"
    assert not s["tickets"]["T1"].get("card_confirmed")


def test_prod_batch_separates_router_ships(proj):
    seed(proj, T1=("await_prod", [], {"title": "老票"}),
         T2=("await_prod", [], {"kind": "business", "authorized": True, "title": "业务票"}))
    out = json.loads(proj.run("prod-batch").stdout)
    s = proj.state()
    assert out["tickets"] == ["T1"] and out["router_ships"] == ["T2"]
    assert [q["summary"] for q in s["queue"] if q["kind"] == "prod"] == ["上 prod：T1"]


def test_prod_batch_only_business_makes_no_item(proj):
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
    # 模拟调度器重派后 worker 交卡：自动确认
    st = proj.state(); st["tickets"]["T1"]["status"] = "kickoff"
    st["tickets"]["T1"]["worker"]["worktree"] = str(proj.tmp / "wt")
    (proj.home / "state.json").write_text(json.dumps(st))
    proj.run("submit-card", "T1", "--card", str(_card(proj)))
    t = proj.state()["tickets"]["T1"]
    assert t["card_confirmed"] is True and t["status"] == "executing"
