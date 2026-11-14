"""v2 终审修复（CLI 侧，真子进程）：关票不复活、无墙可续、退回不可绕、仓外业务票、发版批次、代码面、派单线索、
旧 checkout 可见、看板回话格式、absorbs 校验、requeue 原话、待样本满 7 天、tell 代答标注、released 只收在途票。
夹具全是合成文本，不含真实 uid 和原话。"""
import json

from test_business_cli import (_A1, _biz, _biz_closeable, _daily, _inbox_text, _push_q, _rel, _verdict, calibrate,
                               load, propose)
from test_router_cli import proj, push_main, seed, wait_until  # noqa: F401  (proj 是 fixture)

NO_WALL_WAIT = "墙已全部拆完，等分析 agent 提议结束"


def _items(proj, tid="T1", **match):
    return [q for q in proj.state()["queue"] if q.get("ticket") == tid and all(q.get(k) == v for k, v in match.items())]


def _patch(proj, tid="T1", **fields):
    s = proj.state(); s["tickets"][tid].update(fields)
    (proj.home / "state.json").write_text(json.dumps(s, ensure_ascii=False))


# ---- #2 关掉的业务票不能被判定复活 ---------------------------------------------------------------

def test_closed_business_ticket_is_not_revived_by_verdict(proj):
    _biz(proj, attempts=_A1)
    proj.out("close", "T1", "--rolled-back")                                   # 在途尝试：撤回之后才关得了（残留修复 1）
    r = _verdict(proj, "useless", check=False)
    assert r.returncode == 1 and "已关" in json.loads(r.stdout)["error"]
    out = _daily(proj, {"verdicts": [{"ticket": "T1", "attempt": 1, "verdict": "useless", "touched": 1, "hit_wall": 1,
                                      "continued": 0, "note": "合成"}]})
    t = proj.state()["tickets"]["T1"]
    assert len(out["errors"]) == 1 and t["status"] == "done" and t["attempts"][0]["verdict"] is None
    assert t["closeout"]["worktree"] == "pending" and not (t["worker"].get("branch") or "").endswith("-a2")


# ---- #3 墙拆完之后有路可走：redirect 只看有没有在途尝试；空闲票挂新墙 / 用户回「不关」⇒ 问下一个方向 ---------

def test_redirect_accepts_any_final_verdict_without_inflight_attempt(proj):
    for v in ("useful", "wall_broken_not_retained", "no_entry", "harmful", "useless"):
        _biz(proj, attempts=[{**_A1[0], "verdict": v}])
        out = proj.out("redirect", "T1", "--new-card")
        assert out["ok"] and proj.state()["tickets"]["T1"]["status"] == "queued", v
    for v in (None, "pending_sample"):                                         # 上一次尝试还在等判定 = 在途
        _biz(proj, attempts=[{**_A1[0], "verdict": v}])
        r = proj.run("redirect", "T1", "合成方向", "--as-user", check=False)
        assert r.returncode == 1 and "在途" in json.loads(r.stdout)["error"], v
    for status in ("executing", "await_prod", "queued"):                       # 已派、没 released：在途
        _biz(proj, status=status, attempts=[{**_A1[0], "verdict": "useful"}])
        r = proj.run("redirect", "T1", "--new-card", check=False)
        assert r.returncode == 1 and "在途" in json.loads(r.stdout)["error"], status


def _idle_after_last_wall(proj):
    _biz(proj, attempts=_A1, walls=1)
    _verdict(proj, "useful")
    assert proj.state()["tickets"]["T1"]["waiting_for"] == NO_WALL_WAIT


def test_new_wall_on_idle_business_offers_next_direction(proj):
    _idle_after_last_wall(proj)
    _daily(proj, {"verdicts": [], "walls": [{"ticket": "T1", "text": "合成新墙甲"}]})
    (q,) = _items(proj, topic="next")
    t = proj.state()["tickets"]["T1"]
    assert q["kind"] == "direction" and "下一个方向" in q["summary"] and t["status"] == "postlaunch"
    assert q["id"] in t["waiting_for"]
    _daily(proj, {"walls": [{"ticket": "T1", "text": "合成新墙乙"}]})            # 已经在问：不重复推
    assert len(_items(proj, topic="next")) == 1
    out = proj.out("redirect", "T1", "--new-card")                             # router 出新方向卡（不定方向）：清掉这一项、票回队列
    t = proj.state()["tickets"]["T1"]
    assert q["id"] in out["cleared"] and t["status"] == "queued" and not _items(proj, topic="next")


def test_weekly_wall_on_idle_business_offers_next_direction(proj):
    _idle_after_last_wall(proj)
    calibrate(proj)
    propose(proj, load(mode="weekly", problems=[], maint=[], walls=[{"ticket": "T1", "text": "合成周墙"}]))
    assert len(_items(proj, topic="next")) == 1


def test_answer_next_direction_item_requeues_with_label(proj):
    """v2.1 第 1 条：「要下一个方向吗」归用户——router 代答被拒；用户的回答原样（标用户原话）带给新 worker。"""
    _idle_after_last_wall(proj)
    _daily(proj, {"walls": [{"ticket": "T1", "text": "合成新墙甲"}]})
    (q,) = _items(proj, topic="next")
    r = proj.run("answer", q["id"], "合成方向庚", "--as-router", check=False)
    assert r.returncode == 1 and "--new-card" in json.loads(r.stdout)["error"]
    out = proj.out("answer", q["id"], "合成方向庚")
    t = proj.state()["tickets"]["T1"]
    assert out["ok"] and t["status"] == "queued" and not _items(proj, topic="next")
    inbox = _inbox_text(proj)
    assert "用户原话（逐字）" in inbox and "合成方向庚" in inbox and "router 决定" not in inbox
    assert "合成方向庚" in (proj.home / "decisions.md").read_text()


def test_keep_answer_on_idle_business_offers_next_direction(proj):
    _biz(proj, attempts=[{**_A1[0], "verdict": "useful"}], walls=0, waiting_for=NO_WALL_WAIT)
    cq = _push_q(proj, "T1", "close")
    proj.out("answer", cq, "不关")
    assert len(_items(proj, topic="next")) == 1 and proj.state()["tickets"]["T1"]["status"] == "postlaunch"


def test_new_wall_on_busy_business_does_not_offer(proj):
    _biz(proj, attempts=_A1)                                                    # 尝试 1 还在等判定
    _daily(proj, {"walls": [{"ticket": "T1", "text": "合成墙"}]})
    assert not _items(proj, topic="next")
    _biz(proj, attempts=[{**_A1[0], "verdict": "harmful"}])                     # 有害：等 router 撤回，不问方向
    _push_q(proj, "T1", "harm")
    _daily(proj, {"walls": [{"ticket": "T1", "text": "合成墙"}]})
    assert not _items(proj, topic="next")
    t1 = _biz_closeable(proj)                                                   # 草稿票：用 start，不问方向
    _daily(proj, {"walls": [{"ticket": t1, "text": "合成墙"}]})
    assert not _items(proj, t1, topic="next")


# ---- #4 退回用户之后，router 不能自己换方向；没用计数只在用户定方向时归零 ---------------------------------

def test_redirect_as_router_refused_while_return_unanswered(proj):
    _biz(proj, attempts=_A1, streak=2)
    _verdict(proj)                                                              # 第 3 次没用 ⇒ return
    r = proj.run("redirect", "T1", "合成方向", "--as-router", check=False)
    assert r.returncode == 1 and "--as-user" in json.loads(r.stdout)["error"]
    s = proj.state()
    assert [q["kind"] for q in s["queue"]] == ["return"] and s["tickets"]["T1"]["no_use_streak"] == 3
    assert s["tickets"]["T1"]["status"] == "postlaunch"
    proj.out("redirect", "T1", "合成用户方向", "--as-user")
    assert proj.state()["tickets"]["T1"]["no_use_streak"] == 0


def test_router_redirect_keeps_no_use_streak(proj):
    _biz(proj, attempts=[{**_A1[0], "verdict": "useless"}], streak=3,
         waiting_for="连续 3 次没用，退回用户定夺")                               # 退回项已回（「知道了」），没给方向
    proj.out("redirect", "T1", "--new-card")
    assert proj.state()["tickets"]["T1"]["no_use_streak"] == 3


# ---- #5 仓外改动的业务票：核对过 = 这次尝试上线，进待判定；不关票 ---------------------------------------

def _offrepo_biz(proj):
    _biz(proj, status="await_check", attempts=[{**_A1[0], "verdict": "useless"}],
         solution={"kind": "card", "text": "合成仓外方向", "source": "方向卡 v1（已确认，票级授权）"})
    s = proj.state(); s["tickets"]["T1"]["surfaces"] = ["weekly-report"]
    (proj.home / "state.json").write_text(json.dumps(s, ensure_ascii=False))
    return _push_q(proj, "T1", "handson", "核对 T1 的仓外改动")


def test_offrepo_business_checked_records_attempt_not_closed(proj):
    qid = _offrepo_biz(proj)
    out = proj.out("answer", qid, "T1 核对过了")
    t = proj.state()["tickets"]["T1"]
    assert out["status"] == "postlaunch" and t["status"] == "postlaunch" and not t.get("closeout")
    a = t["attempts"][-1]
    assert a["n"] == 2 and a["verdict"] is None and "合成仓外方向" in a["direction"] and "backups" in a["rollback"]
    assert not _items(proj, kind="handson")
    assert proj.out("walkthrough-done", "T1")["already"] is True                # 再说一次：幂等，不再记尝试
    assert len(proj.state()["tickets"]["T1"]["attempts"]) == 2


def test_offrepo_business_walkthrough_done_same_path(proj):
    _offrepo_biz(proj)
    out = proj.out("walkthrough-done", "T1")
    t = proj.state()["tickets"]["T1"]
    assert out["status"] == "postlaunch" and t["status"] == "postlaunch" and t["attempts"][-1]["verdict"] is None


# ---- #6 发版批次：staging 上还有待走查 / 待放行的票，业务票不能被 router 自己带上 prod ----------------------

def test_prod_batch_holds_business_when_legacy_awaits_walkthrough(proj):
    push_main(proj.root)
    seed(proj, T1=("merged_staging", [], {"title": "老票", "merge_sha": "aaa"}),
         T2=("await_prod", [], {"kind": "business", "authorized": True, "title": "业务票", "attempts": [],
                                "merge_sha": "bbb", "card_confirmed_by": "user"}))
    out = proj.out("prod-batch")
    assert out["router_ships"] == [] and out["queue"]
    assert {b["ticket"]: b["state"] for b in out["batch"]} == {"T1": "待走查", "T2": "可上"}
    (q,) = [q for q in proj.state()["queue"] if q["kind"] == "prod"]
    assert "T1" in q["summary"] and "待走查" in q["summary"] and "T2" in q["summary"]
    proj.out("answer", out["queue"], "先别")                                    # 不是放行：只记原话
    out = proj.out("prod-batch")
    assert out["router_ships"] == []
    proj.out("answer", out["queue"], "放行")                                    # 用户放行整批 ⇒ router 连同老票一起上
    out = proj.out("prod-batch")
    assert out["router_ships"] == ["T1", "T2"] and out["queue"] is None


def test_prod_batch_holds_business_when_legacy_awaits_release(proj):
    push_main(proj.root)
    seed(proj, T1=("await_prod", [], {"title": "老票", "merge_sha": "aaa"}),
         T2=("await_prod", [], {"kind": "business", "authorized": True, "title": "业务票", "attempts": [],
                                "card_confirmed_by": "user"}))
    out = proj.out("prod-batch")
    assert out["router_ships"] == [] and out["tickets"] == ["T1"]
    assert {b["ticket"]: b["state"] for b in out["batch"]} == {"T1": "待放行", "T2": "可上"}


def test_prod_batch_legacy_only_unchanged(proj):
    seed(proj, T1=("await_prod", [], {"title": "老票"}), T3=("merged_staging", [], {"title": "老票三"}))
    out = proj.out("prod-batch")
    assert out == {"ok": True, "queue": out["queue"], "tickets": ["T1"], "router_ships": []}
    assert [q["summary"] for q in proj.state()["queue"] if q["kind"] == "prod"] == ["上 prod：T1"]


# ---- #7 代码面：add --business 收 --surfaces；空代码面按重票 --------------------------------------------

def _add_biz(proj, *extra, check=True):
    return proj.run("add", "--business", "--title", "合成直提", "--segment", "合成人群", "--want", "合成想要",
                    "--wall", "合成墙", "--metric", "合成指标", *extra, check=check)


def test_add_business_accepts_surfaces_and_empty_is_heavy(proj):
    t1 = json.loads(_add_biz(proj, "--surfaces", "llm-config").stdout)["ticket"]
    t2 = json.loads(_add_biz(proj).stdout)["ticket"]
    s = proj.state()["tickets"]
    assert s[t1]["surfaces"] == ["llm-config"] and s[t1]["weight"] == "light"
    assert s[t2]["surfaces"] == [] and s[t2]["weight"] == "heavy"
    r = _add_biz(proj, "--surfaces", "nope", check=False)
    assert r.returncode == 1 and "nope" in json.loads(r.stdout)["error"]
    proj.out("surfaces", t2, "--set", "llm-config", "--why", "方向卡给出了代码面")
    assert proj.state()["tickets"][t2]["weight"] == "light"
    proj.out("surfaces", t2, "--set", "", "--why", "合成：清空")
    assert proj.state()["tickets"][t2]["weight"] == "heavy"


def test_maint_without_surfaces_is_heavy(proj):
    tid = proj.out("maint", "add", "--title", "合成维护", "--problem", "合成", "--surfaces", "")["ticket"]
    t = proj.state()["tickets"][tid]
    assert t["surfaces"] == [] and t["weight"] == "heavy"


# ---- #8 重新排队后的派单：上一次方向标「已试」，列出没拆的墙 ------------------------------------------------

def test_requeue_brief_marks_tried_direction_and_lists_open_walls(proj):
    _biz(proj, attempts=[{**_A1[0], "direction": "合成上线方向甲"}], start_ok=True,
         solution={"kind": "card", "text": "合成卡方向甲", "source": "方向卡 v1（已确认，票级授权）"})
    _verdict(proj)                                                              # 没用 ⇒ 重新排队
    sol = proj.state()["tickets"]["T1"]["solution"]
    assert sol["kind"] == "clue" and "墙1" in sol["text"] and "墙2" in sol["text"]
    assert "已试" in sol["text"] and "合成卡方向甲" in sol["text"] and "没用" in sol["text"]
    proj.out("tick")
    wait_until(lambda: proj.claude())
    argv = proj.claude()[-1]
    prompt = argv[argv.index("-p") + 1]
    assert "已试" in prompt and "没拆的墙" in prompt and "墙1" in prompt


# ---- #9 stale_checkouts 显示在看板和 status ---------------------------------------------------------

def test_status_and_board_show_stale_checkouts(proj):
    _biz(proj, attempts=_A1, stale_checkouts=[{"worktree": "/tmp/合成-wt-1", "branch": "router/T1",
                                               "why": "kept: 有未提交改动", "at": "x"}])
    text = proj.run("status").stdout
    assert "旧 checkout 1 条" in text and "/tmp/合成-wt-1" in text
    proj.run("decide", "--ticket", "T1", "--decision", "合成", "--why", "合成", "--basis", "合成")   # 重画看板
    board = (proj.home / "board.html").read_text()
    assert "旧 checkout 1 条" in board and "/tmp/合成-wt-1" in board


# ---- #10 看板上每类业务项都有回话格式 --------------------------------------------------------------

def test_board_say_for_business_items():
    import board
    say = lambda kind, topic=None: board._say({"id": "Q7", "ticket": "T3", "kind": kind,
                                               **({"topic": topic} if topic else {})})
    assert say("verify_problem") == "Q7 真 / Q7 真，先放着 / Q7 不是：理由"
    assert say("close") == "Q7 关 / Q7 不关"
    assert say("harm") == "Q7 知道了 / Q7：…"
    assert "T3 换成：…" in say("return") and "关 T3" in say("return")
    assert "T3 换成：…" in say("direction", "next")
    assert say("stuck", "requeue") == "Q7 重排 / T3 换成：…"


def test_requeue_item_hint_matches_redirect_for_useful(proj):
    """requeue 项的提示「T 换成：…」= redirect：useful 判定之后排不上队的票也收。"""
    _biz(proj, attempts=_A1)
    _patch(proj, dispatch={"phase": "spawning", "unverified": "合成"})
    _verdict(proj, "useful")
    (q,) = _items(proj, topic="requeue")
    s = proj.state(); s["tickets"]["T1"].pop("dispatch")
    (proj.home / "state.json").write_text(json.dumps(s, ensure_ascii=False))
    out = proj.out("redirect", "T1", "合成用户方向", "--as-user")
    assert q["id"] in out["cleared"] and proj.state()["tickets"]["T1"]["status"] == "queued"


# ---- #11 weekly 提议的 absorbs 校验和 migrate 一致：不静默改挂 ---------------------------------------

def test_propose_absorbs_rejects_parented_non_legacy_and_duplicates(proj):
    seed(proj, T5=("postlaunch", [], {"title": "老票五", "parent": "T7"}), T6=("executing", [], {"title": "老票六"}),
         T7=("executing", [], {"kind": "business", "authorized": True, "title": "业务七", "attempts": [], "walls": [],
                               "absorbs": ["T5"]}))
    for bad in (["T5"], ["T7"]):
        d = load(maint=[]); d["problems"][0]["absorbs"] = bad
        r = propose(proj, d, check=False)
        assert r.returncode == 1 and bad[0] in json.loads(r.stdout)["error"], bad
    d = load(maint=[]); d["problems"][0]["absorbs"] = ["T6"]; d["problems"][1]["absorbs"] = ["T6"]
    r = propose(proj, d, check=False)
    assert r.returncode == 1 and "T6" in json.loads(r.stdout)["error"]
    assert not proj.state().get("proposals")


def test_verify_answer_does_not_reparent_absorbed_legacy(proj):
    seed(proj, T5=("executing", [], {"title": "老票五"}),
         T7=("executing", [], {"kind": "business", "authorized": True, "title": "业务七", "attempts": [], "walls": [],
                               "absorbs": []}))
    d = load(maint=[]); d["problems"][0]["absorbs"] = ["T5"]
    propose(proj, d)
    proj.out("absorb", "T7", "T5")                                              # 判真之前已被并进别处
    qid = [q for q in proj.state()["queue"] if q["kind"] == "verify_problem"][0]["id"]
    out = proj.out("answer", qid, "真，先放着")
    s = proj.state()
    assert s["tickets"]["T5"]["parent"] == "T7" and out["absorb_skipped"] == ["T5"]
    assert s["tickets"][out["ticket"]]["absorbs"] == []


# ---- #12 回 requeue 项：用户原话原样进 inbox，标成用户原话 ---------------------------------------------

def test_requeue_item_answer_passes_user_words_verbatim(proj):
    _biz(proj, attempts=_A1)
    _patch(proj, dispatch={"phase": "spawning", "unverified": "合成"})
    _verdict(proj)
    (q,) = _items(proj, topic="requeue")
    s = proj.state(); s["tickets"]["T1"].pop("dispatch")
    (proj.home / "state.json").write_text(json.dumps(s, ensure_ascii=False))
    proj.out("answer", q["id"], "合成：重排，先试方向辛")
    inbox = _inbox_text(proj)
    assert "用户原话（逐字）：合成：重排，先试方向辛" in inbox and "换方向" in inbox


# ---- #13 待样本满 7 天、碰到改动但没人走到墙 ⇒ 没入口，走 no_entry 唤醒 ----------------------------------

def test_cli_pending_sample_cap_without_wall_hits_wakes_no_entry(proj):
    _biz(proj, attempts=[{**_A1[0], "verdict": "pending_sample", "pending_since": "2026-10-01T00:00:00Z"}])
    out = proj.out("verdict", "T1", "--attempt", "1", "--verdict", "pending_sample", "--touched", "3",
                   "--hit-wall", "0", "--continued", "0", "--note", "合成")
    assert out["actions"] == ["check_entry"] and proj.state()["tickets"]["T1"]["attempts"][0]["verdict"] == "no_entry"
    assert proj.out("tick")["no_entry"] == [{"ticket": "T1", "attempt": 1}]


# ---- #14 tell --as-router：router 自己的话标「router 转述 / 决定」，用户的话照旧是原话 ---------------------

def test_tell_as_router_labels_inbox(proj):
    seed(proj, T1=("executing", [], {"title": "老票", "worker": {"session_id": "s", "worktree": str(proj.tmp), "runs": 1}}))
    proj.out("tell", "T1", "合成转述甲", "--as-router")
    inbox = _inbox_text(proj)
    assert "router 转述 / 决定" in inbox and "合成转述甲" in inbox
    proj.out("tell", "T1", "合成原话乙")
    inbox = _inbox_text(proj)
    assert "合成原话乙" in inbox and "router 转述" not in inbox


def test_tell_resume_as_router_prompt_not_user_words(proj):
    seed(proj, T1=("kickoff", [], {"title": "老票", "worker": {"session_id": "s", "worktree": str(proj.tmp), "runs": 1}}))
    proj.out("tell", "T1", "合成挑战丙", "--resume", "--as-router")
    wait_until(lambda: proj.claude())
    argv = proj.claude()[-1]
    prompt = argv[argv.index("-p") + 1]
    assert "router 转述 / 决定" in prompt and "合成挑战丙" in prompt and "用户原话（逐字）" not in prompt


# ---- #16 业务票的 released 只收在途状态的票 ------------------------------------------------------

def test_released_business_refuses_not_inflight_status(proj):
    for status in ("draft", "done", "queued", "executing", "hold"):
        _biz(proj, status=status)
        r = _rel(proj, "--rollback", "合成撤回", check=False)
        assert r.returncode == 1 and status in json.loads(r.stdout)["error"], status
        assert proj.state()["tickets"]["T1"]["attempts"] == []
    assert not (proj.tmp / "daily" / "state" / "releases.json").exists()
    _biz(proj, status="await_prod")
    assert _rel(proj, "--rollback", "合成撤回").returncode == 0
