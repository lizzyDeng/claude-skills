from datetime import datetime, timedelta, timezone

import pytest

import store
from conftest import make_state
from store import (add_attempt, attach_wall, impact_score, new_business_ticket, record_verdict,
                   ticket_kind)

T0 = "2026-11-01T00:00:00Z"


def _biz(state=None, **kw):
    s = state or store.empty_state()
    args = dict(title="测试问题", segment="测试人群甲", want="想要的行为", walls=["墙一"],
                metric={"name": "m"}, evidence=["证据甲"],
                impact={"size": 0.3, "gap": 0.7, "confidence": "high"}, origin="weekly")
    args.update(kw)
    return s, new_business_ticket(s, **args)


def _go(s, tid, verdict, at=T0, **kw):
    n = add_attempt(s, tid, "方向", at, "回滚方式")
    args = dict(touched=1, hit_wall=1, continued=1, note="n")
    args.update(kw)
    return n, record_verdict(s, tid, n, verdict, at, **args)


def test_legacy_ticket_kind_defaults():
    s = make_state(T1=("draft", []))
    assert ticket_kind(s["tickets"]["T1"]) == "legacy"
    assert "kind" not in store._ticket_defaults()


def test_new_business_ticket_fields():
    s, tid = _biz()
    t = s["tickets"][tid]
    assert ticket_kind(t) == "business"
    assert t["authorized"] is True
    assert t["walls"][0]["id"] == "W1" and t["walls"][0]["status"] == "open"
    assert t["attempts"] == [] and t["tasks"] == [] and t["no_use_streak"] == 0 and t["parent"] is None


def test_impact_score():
    _, tid = _biz()
    s, tid = _biz()
    assert impact_score(s["tickets"][tid]) == pytest.approx(0.21)
    assert impact_score(make_state(T1=("draft", []))["tickets"]["T1"]) == 0.0


def test_attach_wall_numbers_continue():
    s, tid = _biz()
    assert attach_wall(s, tid, "墙二", "analyst", T0) == "W2"
    assert attach_wall(s, tid, "墙三", "analyst", T0) == "W3"


def test_verdict_useful_breaks_wall():
    s, tid = _biz()
    s["tickets"][tid]["no_use_streak"] = 2
    _, r = _go(s, tid, "useful")
    t = s["tickets"][tid]
    assert t["walls"][0]["status"] == "broken"
    assert "next_wall" in r["actions"] and t["no_use_streak"] == 0
    assert t["attempts"][0]["verdict"] == "useful" and t["attempts"][0]["touched"] == 1


def test_verdict_not_retained_requires_new_wall():
    s, tid = _biz()
    n = add_attempt(s, tid, "方向", T0, "rb")
    kw = dict(touched=1, hit_wall=1, continued=0, note="n")
    with pytest.raises(ValueError):
        record_verdict(s, tid, n, "wall_broken_not_retained", T0, **kw)
    r = record_verdict(s, tid, n, "wall_broken_not_retained", T0, new_wall="新墙", **kw)
    t = s["tickets"][tid]
    assert [w["id"] for w in t["walls"]] == ["W1", "W2"]
    assert t["walls"][0]["status"] == "broken" and "next_wall" in r["actions"]


def test_verdict_useless_streak_returns_at_3():
    s, tid = _biz()
    r1 = _go(s, tid, "useless")[1]
    r2 = _go(s, tid, "useless")[1]
    r3 = _go(s, tid, "useless")[1]
    assert r1["actions"] == ["change_direction"] and r2["actions"] == ["change_direction"]
    assert "return_to_user" in r3["actions"]


def test_verdict_harmful_requests_rollback():
    s, tid = _biz()
    assert "rollback" in _go(s, tid, "harmful")[1]["actions"]


def test_no_entry_checks_entry():
    s, tid = _biz()
    assert "check_entry" in _go(s, tid, "no_entry")[1]["actions"]


def test_pending_sample_capped_7_days():
    s, tid = _biz()
    n = add_attempt(s, tid, "方向", T0, "rb")
    kw = dict(touched=0, hit_wall=0, continued=0, note="n")
    r = record_verdict(s, tid, n, "pending_sample", T0, **kw)
    assert r["actions"] == ["wait"]
    later = (datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(days=7, seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(ValueError, match="待样本已满 7 天"):
        record_verdict(s, tid, n, "pending_sample", later, **kw)
    assert s["tickets"][tid]["attempts"][0]["pending_since"] == T0


def test_verdict_refuses_historical_attempt():
    s, tid = _biz()
    s["tickets"][tid]["attempts"].append(
        {"n": 1, "direction": "旧", "shipped_at": T0, "verdict": "useful", "historical": True})
    with pytest.raises(ValueError):
        record_verdict(s, tid, 1, "useful", T0, touched=1, hit_wall=1, continued=1, note="")
    assert add_attempt(s, tid, "新", T0, "rb") == 2


def test_verdict_unknown_attempt_or_verdict():
    s, tid = _biz()
    with pytest.raises(ValueError):
        record_verdict(s, tid, 9, "useful", T0, touched=1, hit_wall=1, continued=1, note="")
    n = add_attempt(s, tid, "方向", T0, "rb")
    with pytest.raises(ValueError):
        record_verdict(s, tid, n, "bogus", T0, touched=1, hit_wall=1, continued=1, note="")
