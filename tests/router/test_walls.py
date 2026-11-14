"""缺口 1：改墙命令 `router.py walls` + 业务票 issue 正文自动同步。

夹具全是合成值：uid 用 5eed 开头的合成串，「原话」是合成句子；不调真 gh（FakeCtx），不起进程。
"""
import argparse
import json

import pytest
from conftest import FIX

import router
import store
import tick

UID = "5eed00a1"                                       # 合成 uid：8 位十六进制、带字母 ⇒ 防泄漏闸认得
QUOTE = "这是一句合成的测试原话专门用来触发防泄漏闸门"     # 合成「原话」：连续 20 字命中即算
MAIN_SHA = "a" * 40


@pytest.fixture
def biz(tmp_path, fake, monkeypatch):
    monkeypatch.delenv("ROUTER_HOME", raising=False)
    monkeypatch.delenv("ROUTER_TICKET", raising=False)
    root = tmp_path / "proj"; home = root / ".claude" / "router"; home.mkdir(parents=True)
    (home / "surfaces.json").write_text((FIX / "surfaces.json").read_text())
    guard = tmp_path / "guard" / "users.json"; guard.parent.mkdir()
    guard.write_text(json.dumps({UID: {"quote": QUOTE}}, ensure_ascii=False))
    runs = tmp_path / "daily" / "runs"; runs.mkdir(parents=True)
    cfg = {"repo": "o/r", "base_branch": "staging", "worktree_dir": ".claude/worktrees", "map_title": "m",
           "weekly_runs_dir": str(runs), "lark_config": str(tmp_path / "lark.json"),
           "leak_guard_globs": [str(guard)], "slots": {"heavy": 0, "light": 0}, "kickoff_budget_min": 30,
           "stale_min": 45, "disk_limit_gb": 150, "claude_projects_dir": str(tmp_path / "projects"),
           "worker": {"model": "m", "permission_mode": "bypassPermissions", "allowed_tools": [], "disallowed_tools": []}}
    (home / "config.json").write_text(json.dumps(cfg, ensure_ascii=False))
    for b in ("staging", "main"):
        fake.script(["git", "-C", str(root), "ls-remote", "--exit-code", "origin", f"refs/heads/{b}"],
                    f"{MAIN_SHA}\trefs/heads/{b}\n")

    class P:
        pass
    p = P(); p.root, p.home, p.cfg, p.ctx, p.tmp = root, home, cfg, fake, tmp_path
    p.state = lambda: store.load_state(home / "state.json")
    p.save = lambda s: store.save_state(home / "state.json", s)
    p.env = lambda: router.Env(root, fake)
    p.tick = lambda: tick.run(fake, root, cfg)
    p.edits = lambda n=41: [c for c in fake.calls_with("issue", "edit") if str(n) in c]
    return p


def seed(p, tid_extra=None, walls=("旧墙一", "旧墙二"), **extra):
    """一张业务票 T1（已上线、空闲，issue #41），map = 5。extra 覆盖票上的字段。"""
    s = store.empty_state()
    tid = store.new_business_ticket(s, title="合成业务票", segment="合成人群", want="合成想要的东西",
                                    walls=list(walls), metric={"name": "合成指标"}, evidence=["/local/ev.json"],
                                    impact={"size": 0.3, "gap": 0.5, "confidence": "mid"}, origin="user")
    t = s["tickets"][tid]
    t.update(status="postlaunch", issue=41, start_ok=True)
    t.update(extra)
    s["map"] = 5
    p.save(s)
    return s


def walls_file(p, data, name="walls.json"):
    f = p.tmp / name
    f.write_text(json.dumps(data, ensure_ascii=False))
    return f


def run_walls(p, data, said="合成原话：照这三道墙改", remap=(), tid="T1"):
    a = argparse.Namespace(ticket=tid, source=str(walls_file(p, data)), said=said, remap=list(remap))
    return router.cmd_walls(a, p.env())


GOOD = {"walls": [{"root_cause": "根因甲：合成", "share": 0.4, "symptoms": ["症状一", "症状二"], "was": ["W2"]},
                  {"root_cause": "根因乙：合成", "share": 0.2, "symptoms": ["症状三"]}],
        "notes": ["不到百分之十的合成现象"]}


# ---- 改墙命令 ------------------------------------------------------------------------------------

def test_walls_replace_keeps_history_decide_inbox_answers(biz):
    seed(biz)
    out = run_walls(biz, GOOD)
    t = biz.state()["tickets"]["T1"]
    assert out["ok"] and out["walls"] == ["W1", "W2"] and out["decision"].startswith("D")
    w1, w2 = t["walls"]
    assert (w1["id"], w1["root_cause"], w1["share"], w1["status"]) == ("W1", "根因甲：合成", 0.4, "open")
    assert [s["text"] for s in w1["symptoms"]] == ["症状一", "症状二"] and w1["text"] == "根因甲：合成"
    assert w2["share"] == 0.2 and w1["was"] == ["W2"]
    hist = t["walls_history"]
    assert len(hist) == 1 and hist[0]["decision"] == out["decision"] and hist[0]["at"]
    assert [w["text"] for w in hist[0]["walls"]] == ["旧墙一", "旧墙二"]                 # 旧墙原样存进历史
    assert t["notes"] == ["不到百分之十的合成现象"]
    dec = (biz.home / "decisions.md").read_text()
    assert out["decision"] in dec and "换墙" in dec
    assert "合成原话：照这三道墙改" in (biz.home / "answers.md").read_text()               # 原话进 answers.md
    inbox = (biz.home / "inbox" / "T1.jsonl").read_text()
    assert "墙改了" in inbox and out["decision"] in inbox                                # worker 收件箱一条「墙改了」
    out2 = run_walls(biz, {"walls": [{"root_cause": "根因丙", "share": 0.5, "symptoms": ["症状四"]}]})
    t = biz.state()["tickets"]["T1"]
    assert len(t["walls_history"]) == 2 and t["walls_history"][1]["decision"] == out2["decision"]
    assert [w["text"] for w in t["walls_history"][1]["walls"]] == ["根因甲：合成", "根因乙：合成"]


def test_walls_v1_kept_as_first_history_entry(biz):
    old = [{"id": "W1", "text": "最早的墙", "status": "open", "source": "analyst", "at": "2026-11-01T00:00:00Z"}]
    seed(biz, walls_v1=old, title_v1="最早的标题", notes=["已有备注"])
    run_walls(biz, GOOD)
    t = biz.state()["tickets"]["T1"]
    first = t["walls_history"][0]
    assert first["source"] == "walls_v1" and first["walls"] == old and first["title"] == "最早的标题"
    assert "walls_v1" not in t and "title_v1" not in t                                  # 挪进历史，不丢
    assert [w["text"] for w in t["walls_history"][1]["walls"]] == ["旧墙一", "旧墙二"]
    assert t["notes"] == ["已有备注", "不到百分之十的合成现象"]


def test_walls_under_ten_percent_refused_points_to_notes(biz):
    seed(biz)
    before = biz.state()
    data = {"walls": [{"root_cause": "根因甲", "share": 0.4, "symptoms": ["症状一"]},
                      {"root_cause": "小根因", "share": 0.06, "symptoms": ["症状二"]}]}
    with pytest.raises(router.Fail) as e:
        run_walls(biz, data)
    assert "10%" in str(e.value) and "notes" in str(e.value) and "W2" in str(e.value)
    assert biz.state()["tickets"]["T1"]["walls"] == before["tickets"]["T1"]["walls"]
    assert not (biz.home / "decisions.md").exists()


@pytest.mark.parametrize("bad, word", [
    ({"walls": [{"share": 0.3, "symptoms": ["s"]}]}, "根因"),
    ({"walls": [{"root_cause": "r", "symptoms": ["s"]}]}, "占流失"),
    ({"walls": [{"root_cause": "r", "share": 0.3}]}, "症状"),
    ({"walls": [{"root_cause": "r", "share": 0.3, "symptoms": []}]}, "症状"),
    ({"walls": [{"root_cause": "r", "share": 1.5, "symptoms": ["s"]}]}, "占流失"),
    ({"walls": []}, "至少一道"),
])
def test_walls_each_wall_needs_root_share_symptoms(biz, bad, word):
    seed(biz)
    with pytest.raises(router.Fail) as e:
        run_walls(biz, bad)
    assert word in str(e.value)


def test_walls_inflight_attempt_wall_removed_needs_remap(biz):
    att = [{"n": 1, "direction": "合成方向", "at": "2026-11-10T00:00:00Z", "rollback": "关开关", "verdict": None,
            "wall": "W1"}]
    seed(biz, attempts=att)
    data = {"walls": [{"root_cause": "根因甲", "share": 0.4, "symptoms": ["症状一"]},
                      {"root_cause": "根因乙", "share": 0.3, "symptoms": ["症状二"], "was": ["W2"]}]}
    with pytest.raises(router.Fail) as e:                                               # W1 在途尝试对准、被删了
        run_walls(biz, data)
    assert "--remap" in str(e.value) and "W1" in str(e.value) and "尝试 1" in str(e.value)
    assert biz.state()["tickets"]["T1"]["walls"][0]["text"] == "旧墙一"
    with pytest.raises(router.Fail):                                                    # 改对准的墙必须是新墙的编号
        run_walls(biz, data, remap=["W1=W9"])
    out = run_walls(biz, data, remap=["W1=W2"])
    t = biz.state()["tickets"]["T1"]
    assert out["remap"] == {"W1": "W2"} and t["attempts"][0]["wall"] == "W2"
    assert "W1" in (biz.home / "decisions.md").read_text()


def test_walls_inflight_attempt_followed_by_was(biz):
    att = [{"n": 1, "direction": "合成方向", "at": "2026-11-10T00:00:00Z", "rollback": "关开关",
            "verdict": "pending_sample", "wall": "W2"},
           {"n": 0, "direction": "并进来的老票", "verdict": "legacy", "historical": True}]
    seed(biz, attempts=att)
    run_walls(biz, GOOD)                                                                # 新 W1 的 was = 旧 W2
    t = biz.state()["tickets"]["T1"]
    assert t["attempts"][0]["wall"] == "W1"


def test_walls_requires_said_and_business(biz):
    seed(biz)
    with pytest.raises(router.Fail) as e:
        run_walls(biz, GOOD, said="  ")
    assert "--said" in str(e.value)
    with pytest.raises(SystemExit):                                                     # 命令行上 --said 必填
        router.build_parser().parse_args(["walls", "T1", "--from", "x.json"])
    s = biz.state()
    s["tickets"]["T1"]["kind"] = None                                                   # 老票：不收
    biz.save(s)
    with pytest.raises(router.Fail):
        run_walls(biz, GOOD)
    s["tickets"]["T1"].update(kind="business", status="done")
    biz.save(s)
    with pytest.raises(router.Fail) as e:
        run_walls(biz, GOOD)
    assert "已关" in str(e.value)
    assert "walls" in router.ROUTER_ONLY                                                # worker 不许改墙


@pytest.mark.parametrize("where", ["root_cause", "symptom", "note", "title"])
def test_walls_leak_guard_refuses(biz, where):
    seed(biz)
    data = json.loads(json.dumps(GOOD, ensure_ascii=False))
    leak = {"root_cause": f"根因带 {UID}", "symptom": f"症状：{QUOTE}", "note": f"备注 {UID}", "title": f"标题{QUOTE}"}[where]
    if where == "root_cause":
        data["walls"][0]["root_cause"] = leak
    elif where == "symptom":
        data["walls"][1]["symptoms"] = [leak]
    elif where == "note":
        data["notes"] = [leak]
    else:
        data["title"] = leak
    with pytest.raises(router.Fail) as e:
        run_walls(biz, data)
    assert "uid" in str(e.value) or "原话" in str(e.value)
    assert biz.state()["tickets"]["T1"]["walls"][0]["text"] == "旧墙一"


def test_walls_title_change_and_offer_next_when_idle(biz):
    att = [{"n": 1, "direction": "合成方向", "at": "2026-11-10T00:00:00Z", "rollback": "关开关", "verdict": "useful",
            "wall": "W1"}]
    seed(biz, attempts=att)
    out = run_walls(biz, {**GOOD, "title": "合成新标题"})
    t = biz.state()["tickets"]["T1"]
    assert t["title"] == "合成新标题" and t["walls_history"][0]["title"] == "合成业务票"
    assert out.get("next_item")                                                        # 空闲票挂了新墙：问下一个方向
    assert t["attempts"][0]["wall"] is None and t["attempts"][0]["wall_history"][0]["wall"] == "W1"


# ---- issue 正文自动同步 ---------------------------------------------------------------------------

def _body(call):
    return call[call.index("--body") + 1]


def test_issue_body_has_walls_and_notes_section(biz):
    seed(biz)
    run_walls(biz, GOOD)
    body = __import__("github").issue_body(5, biz.state()["tickets"]["T1"])
    assert "Part of #5" in body and "## 当前的墙和备注" in body
    assert "根因甲：合成" in body and "40%" in body and "症状一" in body and "不到百分之十的合成现象" in body


def test_tick_syncs_issue_body_when_changed(biz):
    seed(biz)
    biz.tick()
    (first,) = biz.edits()                                                              # 没存过哈希：第一次见就同步
    assert "## 当前的墙和备注" in _body(first) and "--title" in first
    assert biz.state()["tickets"]["T1"]["issue_sync"]["hash"]
    biz.tick()
    assert len(biz.edits()) == 1                                                        # 没变：不再发
    run_walls(biz, {**GOOD, "title": "合成新标题"})
    biz.tick()
    assert len(biz.edits()) == 2
    second = biz.edits()[-1]
    assert "根因甲：合成" in _body(second) and second[second.index("--title") + 1] == "合成新标题"
    run_walls(biz, {"walls": [{"root_cause": "根因丙", "share": 0.5, "symptoms": ["症状四"]}], "title": "合成新标题"})
    biz.tick()
    third = biz.edits()[-1]
    assert "根因丙" in _body(third) and "--title" not in third                          # 标题没变：只改正文


def test_issue_created_at_start_records_hash_no_resync(biz):
    s = seed(biz, status="draft", issue=None)
    biz.ctx.script(["gh", "issue", "create"], "https://github.com/o/r/issues/77\n")
    router._start_locked(biz.env(), s, ["T1"])
    t = s["tickets"]["T1"]
    assert t["issue"] == 77 and t["issue_sync"]["hash"]
    created = biz.ctx.calls_with("issue", "create")[0]
    assert "## 当前的墙和备注" in _body(created)
    biz.save(s)
    biz.tick()
    assert biz.edits(77) == []


def test_merge_and_absorb_trigger_sync(biz):
    s = seed(biz)
    t2 = store.new_business_ticket(s, title="合成业务票二", segment="合成人群", want="合成想要的东西",
                                   walls=["被并票的墙"], metric={"name": "合成指标"}, evidence=[],
                                   impact={}, origin="user")
    s["tickets"][t2].update(status="postlaunch", issue=42, start_ok=True)
    old = store.new_ticket(s, title="合成老票", status="done", issue=43)
    biz.save(s)
    biz.tick()
    assert len(biz.edits(41)) == 1
    env = biz.env()
    with env.state() as st:
        router.merge_business(env, st, "T1", t2, "合成原话：并")
    biz.tick()
    assert len(biz.edits(41)) == 2 and "被并票的墙" in _body(biz.edits(41)[-1])
    assert len(biz.edits(42)) == 1                                                      # 被并的票不再同步（收尾关 issue）
    router.cmd_absorb(argparse.Namespace(ticket="T1", olds=[old]), biz.env())
    biz.tick()
    assert len(biz.edits(41)) == 3 and old in _body(biz.edits(41)[-1])


def test_issue_sync_leak_not_sent_goes_to_decision_queue(biz):
    seed(biz, walls=[f"带 {UID} 的墙"])                                                 # 直接写进票（绕过 walls 命令的闸）
    biz.tick()
    assert biz.edits() == []
    items = [q for q in biz.state()["queue"] if q.get("topic") == "issue_sync"]
    assert len(items) == 1 and items[0]["ticket"] == "T1" and UID not in items[0]["summary"]
    biz.tick()
    assert biz.edits() == [] and len([q for q in biz.state()["queue"] if q.get("topic") == "issue_sync"]) == 1
    run_walls(biz, {"walls": [{"root_cause": "根因甲：合成", "share": 0.4, "symptoms": ["症状一"]}]})   # 改掉之后：同步、撤项
    biz.tick()
    assert len(biz.edits()) == 1 and UID not in " ".join(biz.edits()[0])
    assert not [q for q in biz.state()["queue"] if q.get("topic") == "issue_sync"]


def test_issue_sync_gh_failure_retried_next_round(biz):
    seed(biz)
    biz.ctx.script(["gh", "issue", "edit"], "", returncode=1, stderr="合成：gh 网络错误")
    res = biz.tick()
    assert any("#41" in e and "同步" in e for e in res["errors"])
    assert not (biz.state()["tickets"]["T1"].get("issue_sync") or {}).get("hash")
    biz.ctx.script(["gh", "issue", "edit"], "", returncode=0)
    res = biz.tick()
    assert len(biz.edits()) == 2 and biz.state()["tickets"]["T1"]["issue_sync"]["hash"]
    assert not any("#41" in e for e in res["errors"])
