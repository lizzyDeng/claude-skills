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
    # R3：push_queue 不再收 blocks（读时现算），新增可选 topic
    s = load_state(tmp_path / "state.json")
    q1 = push_queue(s, "T1", "card", "看方向卡", "t0")
    q2 = push_queue(s, None, "start", "派发单已备好（3 张）", "t0")
    q3 = push_queue(s, None, "stuck", "远端 staging 不存在", "t0", topic="base")
    assert (q1, q2, q3) == ("Q1", "Q2", "Q3")
    assert s["queue"][2]["topic"] == "base"
    assert pop_queue(s, "Q1")["summary"] == "看方向卡"
    assert [q["id"] for q in s["queue"]] == ["Q2", "Q3"]
    with pytest.raises(KeyError): pop_queue(s, "Q1")
    with pytest.raises(ValueError): push_queue(s, "T1", "bogus", "x", "t0")


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


# ---- plan v4：R3 / R4 / R5 / R6 / R1 的 store 层 -------------------------------
import json  # noqa: E402

import store  # noqa: E402
from conftest import make_state  # noqa: E402


def test_set_status_defaults_waiting_for_by_status(tmp_path):        # R5 ⑥
    s = make_state(T1=("queued", []))
    want = {store.KICKOFF: "写方向卡", store.EXECUTING: "执行中", store.VERIFYING: "等 CI",
            store.AWAIT_CARD: "等你确认方向卡", store.AWAIT_CHECK: "等你核对仓外改动",
            store.AWAIT_PROD: "等上 prod"}
    for status, text in want.items():
        store.set_status(s, "T1", status, "t0")                   # 不传 waiting_for ⇒ 按状态给默认值
        assert s["tickets"]["T1"]["waiting_for"] == text, status
    store.set_status(s, "T1", store.EXECUTING, "t1", waiting_for="自己写的")
    assert s["tickets"]["T1"]["waiting_for"] == "自己写的"
    store.set_status(s, "T1", store.EXECUTING, "t2", waiting_for="")   # 显式空串仍然能清空
    assert s["tickets"]["T1"]["waiting_for"] == ""


def _item(qid, ticket, kind, **kw):
    return {"id": qid, "ticket": ticket, "kind": kind, "summary": "x", "created_at": "t", **kw}


def test_queue_blocks_count_self_and_dependents():                  # R3 ④⑧
    # 本票没完成 ⇒ 算自己 1 张；本票没 settle ⇒ 依赖它的票算上；本票持锁 ⇒ 排队等它代码面的票算上
    s = make_state(T1=("kickoff", ["weekly-report"]), T2=("queued", [], {"blocked_by": ["T1"]}),
                   T3=("queued", ["weekly-report"]))
    assert store.queue_blocks(s, _item("Q1", "T1", "card")) == 3
    # 本票已 settle（合进 staging）⇒ 依赖它的票不再算，排队等代码面的也不算（MERGED 不持锁）
    s = make_state(T1=("merged_staging", ["weekly-report"]), T2=("queued", [], {"blocked_by": ["T1"]}),
                   T3=("queued", ["weekly-report"]))
    assert store.queue_blocks(s, _item("Q1", "T1", "handson")) == 1
    # 本票不持锁（hold）⇒ 等它代码面的不算；依赖它的仍算（它没 settle）
    s = make_state(T1=("hold", ["weekly-report"]), T2=("queued", [], {"blocked_by": ["T1"]}),
                   T3=("queued", ["weekly-report"]))
    assert store.queue_blocks(s, _item("Q1", "T1", "stuck")) == 2
    # 本票 done / skip ⇒ 自己不算
    s = make_state(T1=("done", ["weekly-report"]))
    assert store.queue_blocks(s, _item("Q1", "T1", "handson")) == 0
    # 已 settle / skip 的下游不算
    s = make_state(T1=("kickoff", ["a"]), T2=("skip", ["a"], {"blocked_by": ["T1"]}),
                   T3=("await_prod", [], {"blocked_by": ["T1"]}))
    assert store.queue_blocks(s, _item("Q1", "T1", "card")) == 1
    # start = 本批 weekly 草稿张数；prod = 等上 prod 的张数
    s = make_state(T1=("draft", [], {"batch": "w1"}), T2=("draft", [], {"batch": "w1"}),
                   T3=("queued", [], {"batch": "w1"}), T4=("await_prod", []), T5=("await_prod", []))
    s["batch"]["id"] = "w1"
    assert store.queue_blocks(s, _item("Q1", None, "start")) == 2
    assert store.queue_blocks(s, _item("Q2", None, "prod")) == 2
    # topic=base：挡住所有还没走完的票（不算 merged / await_prod / done / hold / skip）
    s = make_state(T1=("queued", []), T2=("kickoff", []), T3=("merged_staging", []), T4=("hold", []),
                   T5=("done", []), T6=("skip", []), T7=("draft", []))
    assert store.queue_blocks(s, _item("Q1", None, "stuck", topic="base")) == 3


def test_queue_blocks_dedups_ticket_both_dependent_and_sharing_surface():   # codex U2
    s = make_state(T1=("kickoff", ["weekly-report"]),
                   T2=("queued", ["weekly-report"], {"blocked_by": ["T1"]}),    # 既依赖、又同面
                   T3=("queued", ["weekly-report"]),                           # 只同面
                   T4=("queued", ["llm-config"], {"blocked_by": ["T1"]}))      # 只依赖
    assert store.queue_blocks(s, _item("Q1", "T1", "card")) == 4               # 1（自己）+ {T2, T3, T4}，不是 5


def test_queue_blocks_view_derives_counts_and_start_summary():
    s = make_state(T1=("draft", [], {"batch": "w1"}), T2=("kickoff", ["a"]), T3=("queued", ["a"]))
    s["batch"]["id"] = "w1"
    s["queue"] = [_item("Q1", None, "start", summary="派发单已备好（4 张）", blocks=4),   # 旧快照
                  _item("Q2", "T2", "card", blocks=0)]
    v = store.queue_view(s)
    assert [(q["id"], q["blocks"]) for q in v] == [("Q2", 2), ("Q1", 1)]       # 按 (-blocks, 序号)
    assert next(q for q in v if q["id"] == "Q1")["summary"] == "派发单已备好（1 张）"
    assert s["queue"][0]["blocks"] == 4                                        # 只读：不改 state 本身


def test_load_migrates_legacy_offrepo_merged_to_await_check(tmp_path):     # R6 ⑪
    p = tmp_path / "state.json"
    s = make_state(T1=("merged_staging", ["weekly-report"], {"merge_sha": None, "issue": 944}),
                   T2=("merged_staging", ["a"]),                               # 没有 merge_sha 键
                   T3=("merged_staging", ["b"], {"merge_sha": "abc123"}))       # 仓内，有 sha
    p.write_text(json.dumps(s, ensure_ascii=False))
    before = json.loads(p.read_text())["tickets"]
    t = store.load_state(p)["tickets"]
    assert t["T1"]["status"] == store.AWAIT_CHECK
    assert t["T2"]["status"] == "merged_staging" and t["T3"]["status"] == "merged_staging"
    rest = lambda d: {k: v for k, v in d.items() if k not in ("status", "history", "waiting_for")}
    assert rest(t["T1"]) == rest(before["T1"])                 # 只改状态：不写任何「已通知」/收尾标记
    assert t["T2"] == before["T2"] and t["T3"] == before["T3"]
    store.save_state(p, store.load_state(p))
    assert store.load_state(p)["tickets"]["T1"]["status"] == store.AWAIT_CHECK   # 幂等


def test_push_queue_starts_unnotified_and_legacy_items_count_as_notified(fake):   # R4 ⑦
    import notify
    s = make_state(T1=("kickoff", ["a"]))
    s["queue"] = [_item("Q1", "T1", "card", blocks=1)]           # 老 state 的项：没有 notified_at 键
    s["seq"]["queue"] = 1
    assert notify.flush(s, None, fake, "2026-11-02T10:00:00Z") is None     # 老项算已通知，升级后不补发
    qid = store.push_queue(s, "T1", "stuck", "T1 卡住", "2026-11-02T10:00:00Z")
    new = next(q for q in s["queue"] if q["id"] == qid)
    assert new["notified_at"] is None
    assert notify.flush(s, None, fake, "2026-11-02T10:01:00Z") is not None   # 未配置 Lark：DRY RUN 算送达
    assert new["notified_at"] == "2026-11-02T10:01:00Z"
    assert notify.flush(s, None, fake, "2026-11-02T10:02:00Z") is None


def test_append_decision_with_key_is_idempotent(tmp_path):                 # R1 / codex U7
    d = tmp_path / "decisions.md"
    s = store.load_state(tmp_path / "x.json")
    key = "base:staging:2026-11-02T09:59:00Z:aaaaaaaaaaaa"
    assert store.append_decision(d, s, "-", "远端 staging 不存在：从 origin/main aaaaaaaaaaaa 重建", "授权",
                                 "ls-remote 无 refs/heads/staging", "t0", key=key) == "D1"
    assert store.append_decision(d, s, "-", "同一件事再记一次", "授权", "x", "t1", key=key) == "D1"
    fresh = store.load_state(tmp_path / "x.json")                # 崩在存 state 之前：seq 没前进
    assert store.append_decision(d, fresh, "-", "第三次", "授权", "x", "t2", key=key) == "D1"
    lines = d.read_text().splitlines()
    assert len(lines) == 1 and f"[key:{key}]" in lines[0]
    assert store.decision_with_key(d, key) == "D1" and store.decision_with_key(d, "base:other") is None
    assert store.append_decision(d, s, "-", "另一件", "w", "b", "t3") == "D2"      # 不带 key 照旧每次一行
    assert store.recent_decisions(d)[1]["decision"].startswith("远端 staging 不存在")


# ---- 上线验收（postlaunch）：已上线、等验收；票的终点是上线验收，不是 released ----------------

def test_postlaunch_status_frees_slot_and_lock_and_unblocks_dependents():
    assert store.POSTLAUNCH == "postlaunch" and store.POSTLAUNCH in store.STATUSES
    assert store.POSTLAUNCH not in store.RUNNING and store.POSTLAUNCH not in store.LOCKING   # 代码已在 prod：不占名额、不持锁
    assert store.POSTLAUNCH in store.SETTLED                                                  # 对下游算已解锁
    assert "postlaunch" in store.QUEUE_KINDS
    s = make_state(T1=("postlaunch", ["a"]))
    assert not store.occupies_slot(s["tickets"]["T1"]) and not store.holds_lock(s["tickets"]["T1"])


def test_postlaunch_plan_has_three_checks_from_t0():
    pl = store.postlaunch_plan("2026-11-10T08:30:00Z")
    assert pl == {"t0": "2026-11-10T08:30:00Z",
                  "checks": [{"label": "T+1", "at": "2026-11-11T08:30:00Z"},
                             {"label": "T+3", "at": "2026-11-13T08:30:00Z"},
                             {"label": "T+7", "at": "2026-11-17T08:30:00Z"}],
                  "results": []}


def test_postlaunch_due_only_after_time_and_until_recorded():
    s = make_state(T1=("postlaunch", ["a"], {"postlaunch": store.postlaunch_plan("2026-11-10T08:30:00Z")}))
    t = s["tickets"]["T1"]
    assert store.postlaunch_due(t, "2026-11-11T08:29:59Z") == []
    assert store.postlaunch_due(t, "2026-11-11T08:30:00Z") == ["T+1"]                      # now ≥ at
    assert store.postlaunch_due(t, "2026-11-13T09:00:00Z") == ["T+1", "T+3"]               # 漏了的也照交
    t["postlaunch"]["results"].append({"check": "T+1", "verdict": "看不出来", "summary": "命中 3/40", "action": "",
                                       "at": "2026-11-13T09:00:00Z"})
    assert store.postlaunch_due(t, "2026-11-13T09:00:00Z") == ["T+3"]
    t["status"] = "done"
    assert store.postlaunch_due(t, "2026-11-20T00:00:00Z") == []                           # 只有 postlaunch 的票会到期
