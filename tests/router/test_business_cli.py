import json

from conftest import FIX
from test_router_cli import proj, seed  # noqa: F401  (proj 是 fixture)


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
    assert t["issue"] and t["start_ok"] and t["status"] != "draft"
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
    assert proj.state()["tickets"][t1]["status"] == "done"


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
    assert new["attempts"] == [
        {"n": 1, "direction": "老票五", "shipped_at": "2026-11-01T00:00:00Z", "verdict": "没用", "historical": True},
        {"n": 2, "direction": "老票六", "shipped_at": None, "verdict": "legacy", "historical": True}]


def test_add_business_is_user_verified(proj):
    out = proj.out("add", "--business", "--title", "测试直提", "--segment", "测试人群甲", "--want", "测试想要",
                   "--wall", "测试墙甲", "--wall", "测试墙乙", "--metric", "测试指标")
    t = proj.state()["tickets"][out["ticket"]]
    assert t["kind"] == "business" and t["origin"] == "user" and t["authorized"] is True
    assert [w["text"] for w in t["walls"]] == ["测试墙甲", "测试墙乙"] and t["status"] == "draft"
