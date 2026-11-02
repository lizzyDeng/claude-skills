import pytest

from store import (DRAFT, QUEUED, StateCorrupt, append_decision, load_state, new_ticket,
                   pop_queue, push_queue, recent_decisions, revoke_decision, save_state,
                   set_status)


def test_new_ticket_ids_are_sequential_and_persist(tmp_path):
    p = tmp_path / "state.json"; s = load_state(p)          # 不存在 → 空状态
    assert new_ticket(s, title="a", weight="light") == "T1"
    assert new_ticket(s, title="b", weight="heavy") == "T2"
    save_state(p, s); assert load_state(p)["tickets"]["T2"]["status"] == DRAFT


def test_save_is_atomic_and_corrupt_load_refuses(tmp_path):
    p = tmp_path / "state.json"; p.write_text("{broken")
    with pytest.raises(StateCorrupt): load_state(p)
    assert list(tmp_path.glob("state.json.corrupt-*"))      # 坏文件已备份
    assert p.read_text() == "{broken"                       # 原文件没被覆盖


def test_save_leaves_no_temp_files(tmp_path):
    p = tmp_path / "state.json"; s = load_state(p); new_ticket(s, title="a")
    save_state(p, s); save_state(p, s)
    assert sorted(x.name for x in tmp_path.iterdir()) == ["state.json"]


def test_set_status_appends_history(tmp_path):
    s = load_state(tmp_path / "state.json"); tid = new_ticket(s, title="a")
    set_status(s, tid, QUEUED, "2026-11-02T10:00Z", waiting_for="等 T1")
    t = s["tickets"][tid]
    assert t["history"][-1] == {"at": "2026-11-02T10:00Z", "status": "queued"}
    assert t["waiting_for"] == "等 T1"
    with pytest.raises(ValueError): set_status(s, tid, "bogus", "x")


def test_queue_ids_and_pop(tmp_path):
    s = load_state(tmp_path / "state.json")
    q1 = push_queue(s, "T1", "card", "看方向卡", 2, "t0")
    q2 = push_queue(s, None, "start", "派发单已备好（3 张）", 3, "t0")
    assert (q1, q2) == ("Q1", "Q2")
    assert pop_queue(s, "Q1")["summary"] == "看方向卡"
    assert [q["id"] for q in s["queue"]] == ["Q2"]
    with pytest.raises(KeyError): pop_queue(s, "Q1")
    with pytest.raises(ValueError): push_queue(s, "T1", "bogus", "x", 0, "t0")


def test_decisions_are_numbered_and_revocable(tmp_path):
    d = tmp_path / "decisions.md"; s = load_state(tmp_path / "x.json")
    assert append_decision(d, s, "T3", "排在 T1 之后", "同改 her 回复 prompt", "surfaces.json", "2026-11-02T10:00Z") == "D1"
    revoke_decision(d, "D1", "用户推翻", "2026-11-02T11:00Z")
    assert recent_decisions(d)[0]["revoked"] is True


def test_decision_numbering_survives_unsaved_state(tmp_path):
    # router 写完 decisions.md 但没来得及存 state 就崩了 → 下一条不能撞号
    d = tmp_path / "decisions.md"
    append_decision(d, load_state(tmp_path / "x.json"), "T1", "a", "b", "c", "t0")
    fresh = load_state(tmp_path / "x.json")
    assert append_decision(d, fresh, "T1", "a|b\nc", "w", "x", "t1") == "D2"
    newest = recent_decisions(d)[0]
    assert newest["id"] == "D2" and newest["decision"] == "a｜b c" and newest["revoked"] is False
    with pytest.raises(KeyError): revoke_decision(d, "D9", "x", "t2")
