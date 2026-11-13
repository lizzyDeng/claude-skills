"""v2 终审复审的残留修复（CLI 侧，真子进程、真 git）：
1 关业务票前要等在途尝试判完或先撤回（--rolled-back）；
2 发版批次按 staging 上的实际内容定：合进 staging、还没 released 的票都算（不看现在的状态），
  再和 origin/main..origin/staging 的提交对账，对不上的提交 ⇒ router 不自己上；
3 整批 prod 项在批次不变时原地更新、保留 Q 号。
夹具全是合成文本，不含真实 uid 和原话。"""
import json

from test_business_cli import _A1, _biz, _push_q
from test_router_cli import proj, push_main, seed, staging_commit  # noqa: F401  (proj 是 fixture)


def _err(r):
    return json.loads(r.stdout)["error"]


# ---- 1 关业务票：在途尝试要先判完或撤回 -------------------------------------------------------------

def test_close_refused_while_shipped_attempt_awaits_verdict(proj):
    _biz(proj, attempts=_A1)                                                    # 尝试 1 已上线、还在等 daily 判定
    r = proj.run("close", "T1", check=False)
    assert r.returncode == 1 and "等 daily 判" in _err(r) and "--rolled-back" in _err(r)
    assert proj.state()["tickets"]["T1"]["status"] == "postlaunch"
    cq = _push_q(proj, "T1", "close")                                            # 「Qn 关」同一道闸
    r = proj.run("answer", cq, "关", check=False)
    assert r.returncode == 1 and "--rolled-back" in _err(r)
    s = proj.state()
    assert s["tickets"]["T1"]["status"] == "postlaunch" and cq in [q["id"] for q in s["queue"]]


def test_close_refused_while_change_on_staging_or_offrepo(proj):
    for status in ("await_prod", "await_check", "merged_staging"):
        _biz(proj, status=status, attempts=[{**_A1[0], "verdict": "useless"}])
        r = proj.run("close", "T1", check=False)
        assert r.returncode == 1 and "撤回" in _err(r), status
        assert proj.state()["tickets"]["T1"]["status"] == status


def test_close_rolled_back_closes_and_records_decide(proj):
    _biz(proj, attempts=_A1)
    out = proj.out("close", "T1", "--rolled-back", "--reason", "合成：已撤回")
    t = proj.state()["tickets"]["T1"]
    assert out["ok"] and t["status"] == "done" and t["closeout"]["worktree"] == "pending"
    dec = (proj.home / "decisions.md").read_text()
    assert "已撤回" in dec and "关开关 x" in dec and "关闭业务票" in dec          # 撤回办法记进依据


def test_close_idle_business_needs_no_flag(proj):
    _biz(proj, attempts=[{**_A1[0], "verdict": "useful"}])                      # 判完了、不在 staging：照常关
    assert proj.out("close", "T1")["ok"]
    assert "已撤回" not in (proj.home / "decisions.md").read_text()


# ---- 2 发版批次按 staging 上的实际内容定 ---------------------------------------------------------------

def _legacy_merged(**extra):
    return ("merged_staging", [], {"title": "老票", "merge_sha": "aaa", "pr": 41,
                                   "history": [{"at": "2026-11-13T00:00:00Z", "status": "merged_staging"}],
                                   "worker": {"session_id": "s1", "worktree": "/tmp/合成-wt", "runs": 1}, **extra})


def _biz_ready(**extra):
    return ("await_prod", [], {"kind": "business", "authorized": True, "title": "业务票", "attempts": [],
                               "merge_sha": "bbb", "pr": 42,
                               "history": [{"at": "2026-11-13T00:00:00Z", "status": "await_prod"}], **extra})


def test_takeover_does_not_drop_legacy_from_batch(proj):
    """复审复现：老票 T1 待走查 + 业务票 T2 可上 ⇒ router_ships=[]；T1 被接管（或停掉）后也还是 []。"""
    push_main(proj.root)
    seed(proj, T1=_legacy_merged(), T2=_biz_ready())
    assert proj.out("prod-batch")["router_ships"] == []
    proj.out("takeover", "T1")
    out = proj.out("prod-batch")
    assert out["router_ships"] == [] and out["queue"]
    row = next(b for b in out["batch"] if b["ticket"] == "T1")
    assert row["status"] == "takeover" and "staging" in row["state"]
    proj.out("stop", "T1")
    assert proj.out("prod-batch")["router_ships"] == []


def test_released_ticket_leaves_batch(proj):
    push_main(proj.root)
    seed(proj, T1=_legacy_merged(status="postlaunch", history=[{"at": "x", "status": "merged_staging"},
                                                               {"at": "x", "status": "await_prod"},
                                                               {"at": "x", "status": "postlaunch"}]),
         T3=("done", [], {"title": "老票三（老版 released 直接记 done）", "merge_sha": "ccc",
                          "history": [{"at": "x", "status": "merged_staging"}, {"at": "x", "status": "await_prod"},
                                      {"at": "x", "status": "done"}]}),
         T2=_biz_ready())
    out = proj.out("prod-batch")
    assert [b["ticket"] for b in out["batch"]] == ["T2"] and out["router_ships"] == ["T2"]


def test_unattributed_staging_commit_blocks_router_ship(proj):
    push_main(proj.root)
    staging_commit(proj.root, "合成：业务票的改动 (#42)")                       # 按 PR 号对到 T2
    other = staging_commit(proj.root, "合成：同事的改动")                       # 对不上任何票
    seed(proj, T2=_biz_ready())
    out = proj.out("prod-batch")
    assert out["router_ships"] == [] and out["queue"] and out["verified"] is True
    assert [(c["sha"], c["subject"]) for c in out["commits"]] == [(other, "合成：同事的改动")]
    (q,) = [q for q in proj.state()["queue"] if q["kind"] == "prod"]
    assert other[:12] in q["summary"] and "对不上" in q["summary"]
    proj.out("answer", out["queue"], "放行")                                   # 用户放行整批（连这条提交）⇒ 能上
    out = proj.out("prod-batch")
    assert out["router_ships"] == ["T2"] and out["queue"] is None


def test_merge_commit_needs_exact_sha(proj):
    """merge 提交（多个父提交）只认 sha，标题里有 (#N) 也不算。"""
    import subprocess
    push_main(proj.root)
    g = lambda *a: subprocess.run(["git", *a], cwd=proj.root, check=True, capture_output=True, text=True).stdout
    g("checkout", "-q", "-b", "side")
    g("commit", "-q", "--allow-empty", "-m", "合成：侧枝")
    g("checkout", "-q", "-")
    g("merge", "-q", "--no-ff", "-m", "合成：回合 (#42)", "side")
    g("push", "-q", "origin", "HEAD:staging")
    seed(proj, T2=_biz_ready())
    out = proj.out("prod-batch")
    assert out["router_ships"] == [] and len(out["commits"]) == 2              # 回合提交和侧枝提交都对不上


def test_all_attributed_and_ok_router_ships(proj):
    push_main(proj.root)
    staging_commit(proj.root, "合成：老票改动 (#41)")                           # 按 PR 号对到 T1
    sha = staging_commit(proj.root, "合成：业务票改动")                         # 按合并 sha 对到 T2
    seed(proj, T1=_legacy_merged(status="await_prod", merge_sha="zzz", prod_ok="2026-11-13T00:00:00Z"),
         T2=_biz_ready(merge_sha=sha))
    out = proj.out("prod-batch")
    assert out["router_ships"] == ["T1", "T2"] and out["queue"] is None and out["commits"] == []


def test_unverifiable_staging_blocks_router_ship(proj):
    seed(proj, T2=_biz_ready())                                                # 远端没有 main：核对不了 ⇒ 不自己上
    out = proj.out("prod-batch")
    assert out["router_ships"] == [] and out["verified"] is False and out["queue"]
    (q,) = [q for q in proj.state()["queue"] if q["kind"] == "prod"]
    assert "核对不了" in q["summary"]


# ---- 3 整批 prod 项：批次不变就原地更新、保留 Q 号 -------------------------------------------------------

def test_batch_prod_item_keeps_id_while_batch_unchanged(proj):
    push_main(proj.root)
    seed(proj, T1=_legacy_merged(), T2=_biz_ready(),
         T5=("postlaunch", [], {"title": "老票五（已上线，重上配置）", "postlaunch": {
             "t0": "2026-11-01T00:00:00Z", "checks": [], "results": []}}))
    q1 = proj.out("prod-batch")["queue"]
    assert proj.out("prod-batch")["queue"] == q1
    proj.out("walkthrough-done", "T1")                                          # 状态变了、批次没变：同一个 Q 号，文案更新
    out = proj.out("prod-batch")
    (q,) = [q for q in proj.state()["queue"] if q["kind"] == "prod"]
    assert out["queue"] == q1 == q["id"] and "T1 老票·待放行" in q["summary"]
    proj.out("released", "--tickets", "T5", "--what", "合成重上", "--metrics", "m1")   # 批次外的票 released：不动这一项
    assert [q["id"] for q in proj.state()["queue"] if q["kind"] == "prod"] == [q1]
    proj.out("answer", q1, "放行")                                              # 按看到的号回「放行」照样生效
    assert proj.out("prod-batch")["router_ships"] == ["T1", "T2"]


def test_batch_prod_item_new_id_when_batch_changes(proj):
    push_main(proj.root)
    seed(proj, T1=_legacy_merged(), T2=_biz_ready())
    q1 = proj.out("prod-batch")["queue"]
    s = proj.state()
    s["tickets"]["T3"] = {**s["tickets"]["T1"], "id": "T3", "title": "老票三", "merge_sha": "ddd", "pr": 43}
    (proj.home / "state.json").write_text(json.dumps(s, ensure_ascii=False))
    q2 = proj.out("prod-batch")["queue"]
    assert q2 != q1 and [q["id"] for q in proj.state()["queue"] if q["kind"] == "prod"] == [q2]
