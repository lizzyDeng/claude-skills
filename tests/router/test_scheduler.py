from scheduler import pick_dispatch

SLOTS = {"heavy": 3, "light": 2}


def q(surfaces, weight="light", **kw):
    return ("queued", surfaces, {"weight": weight, "start_ok": True, **kw})


def test_only_started_queued_unblocked_unlocked_tickets(state_with):
    # T1 queued start_ok；T2 queued 但 start_ok=False；T3 被未 settle 的 T4 挡
    st = state_with(T1=q(["a"]), T2=q(["b"], start_ok=False), T3=q(["c"], blocked_by=["T4"]),
                    T4=("executing", ["d"], {"weight": "light"}))
    assert pick_dispatch(st, SLOTS) == ["T1"]


def test_blocker_settled_unblocks(state_with):
    st = state_with(T3=q(["c"], blocked_by=["T4"]), T4=("merged_staging", ["d"]))
    assert pick_dispatch(st, SLOTS) == ["T3"]


def test_external_sessions_consume_heavy_slots(state_with):
    # 3 个外部 heavy session → heavy 名额 0 → 只派 light
    ext = {f"/wt/{i}": {"surfaces": [f"x{i}"], "weight": "heavy", "active": True, "locking": True}
           for i in range(3)}
    st = state_with(external=ext, T1=q(["a"], "heavy"), T2=q(["b"], "light"))
    assert pick_dispatch(st, SLOTS) == ["T2"]


def test_idle_external_does_not_consume_slot(state_with):
    ext = {f"/wt/{i}": {"surfaces": [f"x{i}"], "weight": "heavy", "active": i > 0, "locking": True}
           for i in range(3)}
    st = state_with(external=ext, T1=q(["a"], "heavy"))
    assert pick_dispatch(st, SLOTS) == ["T1"]


def test_awaiting_workers_do_not_consume_slots_but_keep_locks(state_with):
    # T5 await_card 持 her-reply-prompt；T6 queued heavy 也要它 → 不派；T7 queued heavy 无冲突 → 派
    st = state_with(T5=("await_card", ["her-reply-prompt"], {"weight": "heavy"}),
                    T10=("executing", ["e1"], {"weight": "heavy"}),
                    T11=("kickoff", ["e2"], {"weight": "heavy"}),
                    T6=q(["her-reply-prompt"], "heavy"), T7=q(["cast-data"], "heavy"))
    assert pick_dispatch(st, SLOTS) == ["T7"]


def test_priority_then_id_order_and_greedy_lock_claim(state_with):
    # T8、T9 都要 weekly-report，T9 优先级更高 → 只派 T9
    st = state_with(T8=q(["weekly-report"], priority=2), T9=q(["weekly-report"], priority=1),
                    T12=q(["z"], priority=2))
    assert pick_dispatch(st, SLOTS) == ["T9", "T12"]


def test_numeric_id_order_not_lexical(state_with):
    st = state_with(T10=q(["a"]), T2=q(["b"]))
    assert pick_dispatch(st, {"heavy": 0, "light": 1}) == ["T2"]


def test_heavy_blocked_by_disk_skips_heavy_only(state_with):
    st = state_with(T1=q(["a"], "heavy"), T2=q(["b"], "light"))
    assert pick_dispatch(st, SLOTS, heavy_blocked=True) == ["T2"]
