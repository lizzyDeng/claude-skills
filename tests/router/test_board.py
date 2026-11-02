from datetime import datetime, timezone

from conftest import make_state

from board import render

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


def test_board_shows_pending_solution_as_clue_not_plan():
    st = make_state(T1=("queued", ["weekly-report"], {"title": "周报口径", "solution": {"kind": "clue", "text": "拆 tells", "source": "router 分诊"}}))
    html = render(st, [], {}, NOW)
    assert "非方案" in html and 'http-equiv="refresh" content="30"' in html
    assert "拆 tells" in html and "方案待出" in html


def test_board_shows_card_solution_with_version():
    st = make_state(T1=("await_card", ["a"], {"solution": {"kind": "card", "text": "口径版本号", "source": "方向卡 v2（待你确认）"}}))
    html = render(st, [], {}, NOW)
    assert "方向卡 v2（待你确认）" in html and "非方案" not in html


def test_board_escapes_html_and_sorts_queue_by_blocks():
    st = make_state(T1=("queued", ["a"], {"title": "<script>alert(1)</script>"}))
    st["queue"] = [{"id": "Q1", "ticket": "T1", "kind": "card", "summary": "少挡的", "blocks": 0, "created_at": "t"},
                   {"id": "Q2", "ticket": None, "kind": "start", "summary": "多挡的 <b>", "blocks": 4, "created_at": "t"}]
    html = render(st, [], {}, NOW)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert html.index("多挡的 &lt;b&gt;") < html.index("少挡的")


def test_board_shows_review_flag_timeline_subscriptions_and_takeover():
    st = make_state(T1=("executing", ["her-reply-prompt"], {"review_flag": True, "tags": ["底座"], "blocked_by": ["T9"],
                                                            "worker": {"session_id": "sid-7", "worktree": "/wt/t1"}}))
    evs = {"T1": [{"ts": "2026-11-02T09:00:00Z", "type": "finding", "summary": "发现 A"},
                  {"ts": "2026-11-02T09:10:00Z", "type": "progress", "phase": "card_ready"}]}
    html = render(st, [], evs, NOW)
    for part in ("需复核", "发现 A", "card_ready", "订阅", "底座", "T9", "claude --resume sid-7"):
        assert part in html


def test_board_lists_external_sessions_and_decisions():
    st = make_state(external={"/wt/her-attention-stage": {"branch": "feat/her-attention-by-stage", "surfaces": ["her-reply-prompt"],
                                                          "weight": "heavy", "active": True, "locking": True,
                                                          "ledger_mtime": "2026-11-02T09:50:00Z"}})
    decs = [{"id": "D2", "at": "t", "ticket": "T1", "decision": "排在 T3 之后", "why": "同改 prompt", "basis": "x",
             "revoked": False}, {"id": "D1", "at": "t", "ticket": "T1", "decision": "旧决定", "why": "w", "basis": "b", "revoked": True}]
    html = render(st, decs, {}, NOW)
    assert "feat/her-attention-by-stage" in html and "你的 session" in html
    assert "推翻 D2" in html and "已撤回" in html
