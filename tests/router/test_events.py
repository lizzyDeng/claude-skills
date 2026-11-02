import json

import pytest

from events import (STOP_TYPES, TYPES, deliver, emit, emit_progress, mark_review, read_inbox,
                    read_new, route)
from store import load_state, save_state

AT = "2026-11-02T10:00Z"


@pytest.fixture
def st(state_with):
    return state_with(
        T1=("executing", ["her-reply-prompt"]),
        T2=("await_card", ["her-reply-prompt"]),
        T3=("queued", ["cast-data"], {"tags": ["llm_config chat 链"]}),
        T4=("queued", ["weekly-report"], {"blocked_by": ["T1"]}),
        T5=("executing", ["other"]),
        T6=("merged_staging", ["her-reply-prompt"]))


def test_types_are_exactly_five():
    assert TYPES == {"decision", "assumption_broken", "overreach", "interface_changed", "finding"}
    assert STOP_TYPES == {"decision", "assumption_broken", "overreach"}


def test_emit_rejects_unknown_type_and_long_summary(tmp_path):
    with pytest.raises(ValueError): emit(tmp_path, "T1", "progress", "x", "y", at=AT)
    with pytest.raises(ValueError): emit(tmp_path, "T1", "finding", "1\n2\n3\n4", "y", at=AT)
    ev = emit(tmp_path, "T1", "decision", "要跑 40 次真 LLM", "bench.sh", at=AT)
    assert ev["stop"] is True and ev["affects"] == {"surfaces": [], "tags": [], "tickets": []}
    line = (tmp_path / "events" / "T1.jsonl").read_text().splitlines()[-1]
    assert json.loads(line)["summary"] == "要跑 40 次真 LLM"


def test_emit_progress_only_known_phases(tmp_path):
    assert emit_progress(tmp_path, "T1", "card_ready", AT)["type"] == "progress"
    with pytest.raises(ValueError): emit_progress(tmp_path, "T1", "done", AT)


def test_route_matches_surface_tag_and_dependency(st, tmp_path):
    # T1 发 interface_changed，surfaces=["her-reply-prompt"]；T2 锁着它 → 命中；
    # T3 tags 含 "llm_config chat 链" → 命中；T4 blocked_by=[T1] → 命中；T5 无关、T6 已合 → 不命中
    ev = emit(tmp_path, "T1", "interface_changed", "底座前缀改名", "prompt_template_en.rs:48",
              surfaces=["her-reply-prompt"], tags=["llm_config chat 链"], at=AT)
    assert route(st, ev) == (["T2", "T3", "T4"], [], True)


def test_route_named_ticket_hits(st, tmp_path):
    ev = emit(tmp_path, "T2", "finding", "T5 的测试夹具过期", "x.rs:1", tickets=["T5"], at=AT)
    assert route(st, ev) == (["T5"], [], True)


def test_interface_change_defaults_to_sender_surfaces(st, tmp_path):
    ev = emit(tmp_path, "T1", "interface_changed", "函数签名改了", "a.rs:1", at=AT)
    assert route(st, ev)[0] == ["T2", "T4"]


def test_route_includes_external_session_on_same_surface(st, tmp_path):
    st["external"]["/wt/a"] = {"surfaces": ["her-reply-prompt"], "weight": "heavy", "active": True, "locking": True}
    st["external"]["/wt/b"] = {"surfaces": ["other"], "weight": "light", "active": True, "locking": True}
    ev = emit(tmp_path, "T5", "finding", "x", "y", surfaces=["her-reply-prompt"], at=AT)
    assert route(st, ev) == (["T1", "T2"], ["/wt/a"], True)


def test_unmatched_event_goes_to_slow_path(st, tmp_path):
    ev = emit(tmp_path, "T5", "finding", "周报口径有个新问题", "weekly.py:43", at=AT)
    assert route(st, ev) == ([], [], False)


def test_routing_is_idempotent_across_restarts(tmp_path, state_with):
    st = state_with(T1=("executing", ["a"])); p = tmp_path / "state.json"; d = tmp_path
    emit(d, "T1", "finding", "x", "y", at=AT); emit_progress(d, "T1", "card_ready", AT)
    first = read_new(d, st, "T1"); save_state(p, st)
    assert [e["type"] for e in first] == ["finding", "progress"]
    st2 = load_state(p); assert read_new(d, st2, "T1") == []   # 不会重复投递
    emit(d, "T1", "finding", "z", "y", at=AT)
    assert [e["summary"] for e in read_new(d, st2, "T1")] == ["z"]


def test_partial_trailing_line_is_not_consumed(tmp_path, state_with):
    st = state_with(T1=("executing", ["a"]))
    f = tmp_path / "events" / "T1.jsonl"; f.parent.mkdir()
    f.write_text('{"type":"finding","summary":"a"}\n{"type":"fin')       # worker 正写到一半
    assert [e["summary"] for e in read_new(tmp_path, st, "T1")] == ["a"]
    with f.open("a") as h: h.write('ding","summary":"b"}\n')
    assert [e["summary"] for e in read_new(tmp_path, st, "T1")] == ["b"]


def test_inbox_cursor_advances(tmp_path, state_with):
    st = state_with(T2=("executing", ["a"]))
    deliver(tmp_path, "T2", {"type": "finding", "summary": "s1"})
    assert [e["summary"] for e in read_inbox(tmp_path, st, "T2")] == ["s1"]
    assert read_inbox(tmp_path, st, "T2") == []
    deliver(tmp_path, "T2", {"type": "tell", "summary": "s2"})
    assert [e["summary"] for e in read_inbox(tmp_path, st, "T2")] == ["s2"]


def test_mark_review(st):
    mark_review(st, ["T2", "T3"])
    assert [t for t, v in st["tickets"].items() if v["review_flag"]] == ["T2", "T3"]
