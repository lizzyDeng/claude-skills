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
    # 存下的 blocks 与读时现算的一致（R3 之后看板按现算值排）：Q1 的票已完成 = 0，Q2 的票挡 1 + 3 张
    st = make_state(T1=("done", ["a"], {"title": "<script>alert(1)</script>"}),
                    T2=("kickoff", ["b"]), T3=("queued", ["b"]), T4=("queued", [], {"blocked_by": ["T2"]}),
                    T5=("queued", [], {"blocked_by": ["T2"]}))
    st["queue"] = [{"id": "Q1", "ticket": "T1", "kind": "card", "summary": "少挡的", "blocks": 0, "created_at": "t"},
                   {"id": "Q2", "ticket": "T2", "kind": "card", "summary": "多挡的 <b>", "blocks": 4, "created_at": "t"}]
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


# ---- plan v4：R6 仓外票显示、R4/R1 横幅、新 topic 的「你说」、接管命令的显示条件 ----------
import re  # noqa: E402


def _card_of(html, tid):
    """→ (分组 data-g, 这张票卡片的 HTML)。按 `<div class="issue"` 切块，取含「· T」票号的那块。"""
    for chunk in html.split('<div class="issue" ')[1:]:
        if re.search(r"· " + tid + r"</span>", chunk):
            return re.match(r'data-g="(\w+)"', chunk).group(1), chunk
    raise AssertionError(f"看板上没有 {tid} 的卡片")


def test_board_offrepo_ticket_not_shown_as_merged_staging():           # ⑪
    st = make_state(T1=("await_check", ["weekly-report"], {"title": "周报口径", "merge_sha": None}),
                    T2=("merged_staging", ["her-reply-prompt"], {"merge_sha": "abc123"}))
    html = render(st, [], {}, NOW)
    g, card = _card_of(html, "T1")
    assert g == "you" and "仓外已改 · 待你核对" in card and "已合 staging" not in card
    _, card2 = _card_of(html, "T2")
    assert "已合 staging" in card2                                       # 仓内合了的仍叫「已合 staging」
    stats = html[html.index('<div class="stats">'):html.index('<div class="slots">')]
    assert "已合 / 已改完" in stats and "已合 staging" not in stats


def test_board_banners_and_say_for_new_topics():
    st = make_state(T1=("await_check", ["weekly-report"]), T2=("verifying", ["a"]), T3=("queued", ["b"]))
    st["notify_error"] = {"at": "2026-11-02T09:59:00Z", "error": "failed: boom-lark-503", "count": 2}
    st["base_problem"] = {"qid": "Q4", "kind": "foreign_tip"}
    st["queue"] = [
        {"id": "Q1", "ticket": "T1", "kind": "handson", "topic": "offrepo", "summary": "核对 T1", "created_at": "t"},
        {"id": "Q2", "ticket": "T2", "kind": "stuck", "topic": "retarget", "summary": "改回失败", "created_at": "t"},
        {"id": "Q3", "ticket": "T3", "kind": "stuck", "topic": "dispatch", "summary": "派发失败", "created_at": "t"},
        {"id": "Q4", "ticket": None, "kind": "stuck", "topic": "base", "summary": "staging 指向 ffffffffffff", "created_at": "t"}]
    html = render(st, [], {}, NOW)
    for say in ("T1 核对过了", "T1 不满意：", "Q2 改好了", "Q3 重派", "Q4 好了"):
        assert say in html, say
    top = html[:html.index("<h2>等你</h2>")]                            # 横幅在「等你」之前
    assert "boom-lark-503" in top                                        # Lark 发不出去：要看得见
    assert "Q4" in top                                                   # base 出问题：顶部点名那条决定单
    # 票卡：收尾逐目标显示（未完成的目标、保留的原因）
    st = make_state(T1=("done", ["weekly-report"], {"closeout": {
        "downstream": {"T2": {"issue": 945, "state": "done"}, "T3": {"issue": 946, "state": "pending"}},
        "issue": "done", "worktree": "done", "branch": "kept: 1 个未推送提交"}}))
    _, card = _card_of(render(st, [], {}, NOW), "T1")
    assert "收尾未完成" in card and "T3" in card and "1 个未推送提交" in card


def test_board_hides_takeover_for_cleared_or_done_worker():
    st = make_state(T1=("executing", ["a"], {"worker": {"session_id": "sid-1", "worktree": None}}),     # 指针已清
                    T2=("done", ["b"], {"worker": {"session_id": "sid-2", "worktree": "/wt/t2"}}),     # 已完成
                    T4=("await_prod", ["d"], {"worker": {"session_id": "sid-4", "worktree": "/wt/t4"}}),
                    T3=("executing", ["c"], {"worker": {"session_id": "sid-3", "worktree": "/wt/t3"}}))  # 对照：照常显示
    html = render(st, [], {}, NOW)
    assert "cd None" not in html
    for sid in ("sid-1", "sid-2", "sid-4"):
        assert f"claude --resume {sid}" not in html, sid
    assert "cd /wt/t3 &amp;&amp; claude --resume sid-3" in html


# ---- 上线验收（postlaunch）：下一次检查的时间 + 最近一次结论 ------------------------------

def test_board_shows_postlaunch_next_check_last_result_and_item():
    import store
    pl1 = store.postlaunch_plan("2026-11-01T08:00:00Z")
    pl1["results"] = [{"check": "T+1", "verdict": "没入口", "summary": "命中 0 人：开关没开", "action": "照授权打开开关",
                       "at": "2026-11-02T09:00:00Z"}]
    pl2 = store.postlaunch_plan("2026-10-20T08:00:00Z")
    pl2["results"] = [{"check": "T+1", "verdict": "生效", "summary": "转化 2.0% → 2.6%", "action": "",
                       "at": "2026-10-21T09:00:00Z"}]
    st = make_state(T1=("postlaunch", ["a"], {"postlaunch": pl1, "worker": {"session_id": "sid-1", "worktree": "/wt/t1"}}),
                    T2=("done", ["b"], {"postlaunch": pl2}))
    st["queue"] = [{"id": "Q1", "ticket": "T1", "kind": "postlaunch", "summary": "T1 上线验收 T+1 没入口", "created_at": "t"}]
    html = render(st, [], {}, NOW)
    g, card = _card_of(html, "T1")
    assert g != "ok"                                                     # 没验收完不算「已完成」
    assert "已上线" in card and "T+3" in card and "11-04 08:00" in card  # 下一次检查
    assert "没入口" in card and "命中 0 人：开关没开" in card              # 最近一次结论
    assert "claude --resume sid-1" not in card                           # 没有 worker 可接管
    _, card2 = _card_of(html, "T2")
    assert "生效" in card2 and "转化 2.0% → 2.6%" in card2
    assert "上线验收" in html and "Q1 知道了" in html                      # 决定单：类型名 + 你回什么
