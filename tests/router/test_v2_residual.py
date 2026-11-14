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
    """残留 2：闸看改动在不在（合并记录 + 状态历史），夹具照真实 state 带上 _settle 记的 merge_sha 和 history。"""
    for status in ("await_prod", "await_check", "merged_staging"):
        sha = None if status == "await_check" else "bbb0001"
        _biz(proj, status=status, attempts=[{**_A1[0], "verdict": "useless"}], merge_sha=sha, pr=42,
             history=[{"at": "2026-11-13T00:00:00Z", "status": status}])
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
                               "merge_sha": "bbb", "pr": 42, "card_confirmed_by": "user",   # v2.1：方向卡经用户确认才算可上
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


# ==== 终审残留 2 ============================================================================================
# 1 关票闸看改动在不在（staging_pending / 仓外 history），stop / takeover 绕不过；
# 2 main → staging 的回合并提交不挡发版（单列 sync_merges）；
# 3 停 / 接管 / 关前已撤回的票不进 router_ships，released 对它们只记 shipped_at 或跳过；
# 4 --rolled-back 的撤回说明指向这次在途的改动；prod-batch 输出 staging_sha。夹具全是合成文本。

H_AP = [{"at": "2026-11-13T00:00:00Z", "status": "await_prod"}]
H_AC = [{"at": "2026-11-13T00:00:00Z", "status": "await_check"}]
_JUDGED = [{**_A1[0], "verdict": "useless", "rollback": "合成：第一次的撤回办法"}]


def _git(proj, *a):
    import subprocess
    return subprocess.run(["git", *a], cwd=proj.root, check=True, capture_output=True, text=True).stdout.strip()


def test_close_gate_not_bypassed_by_stop(proj):
    """① 改动在 staging 上（await_prod）的业务票：stop 之后 close / 「Qn 关」照样拒；③ --rolled-back 才收。"""
    _biz(proj, status="await_prod", attempts=_JUDGED, merge_sha="bbb0001", pr=42, history=list(H_AP))
    assert proj.run("close", "T1", check=False).returncode == 1
    proj.out("stop", "T1")
    r = proj.run("close", "T1", check=False)
    assert r.returncode == 1 and "--rolled-back" in _err(r) and "staging" in _err(r)
    assert proj.state()["tickets"]["T1"]["status"] == "paused"
    cq = _push_q(proj, "T1", "close")
    r = proj.run("answer", cq, "关", check=False)
    assert r.returncode == 1 and "--rolled-back" in _err(r)
    assert proj.out("close", "T1", "--rolled-back")["ok"]
    assert proj.state()["tickets"]["T1"]["status"] == "done"


def test_close_gate_not_bypassed_by_takeover_offrepo(proj):
    """② 仓外改动（await_check）的业务票：takeover 之后 close 照样拒；③ --rolled-back 才收，撤回办法 = 从备份恢复。"""
    _biz(proj, status="await_check", attempts=_JUDGED, merge_sha=None, history=list(H_AC))
    proj.out("takeover", "T1")
    r = proj.run("close", "T1", check=False)
    assert r.returncode == 1 and "--rolled-back" in _err(r) and "仓外" in _err(r)
    assert proj.state()["tickets"]["T1"]["status"] == "takeover"
    assert proj.out("close", "T1", "--rolled-back")["ok"]
    dec = (proj.home / "decisions.md").read_text()
    assert "backups/T1" in dec and "合成：第一次的撤回办法" not in dec


def test_offrepo_change_judged_no_longer_inflight(proj):
    """仓外这次改动核对过、上线判完（history 里 await_check 之后有 postlaunch）⇒ 不算在途，照常关。"""
    _biz(proj, status="paused", attempts=_JUDGED, merge_sha=None,
         history=[*H_AC, {"at": "x", "status": "postlaunch"}, {"at": "x", "status": "paused"}])
    assert proj.out("close", "T1")["ok"]


def test_rolled_back_decide_names_this_inflight_change(proj):
    """撤回说明指向这次在 staging 上的改动（PR / 合并 sha），不是上一次已判完的尝试的撤回办法。"""
    _biz(proj, status="await_prod", attempts=_JUDGED, merge_sha="bbb0001", pr=42, history=list(H_AP))
    r = proj.run("close", "T1", check=False)
    assert "#42" in _err(r) and "bbb0001" in _err(r)
    proj.out("close", "T1", "--rolled-back")
    dec = (proj.home / "decisions.md").read_text()
    block = dec[dec.index("关票前已撤回"):]
    assert "#42" in block and "bbb0001" in block and "合成：第一次的撤回办法" not in dec


def _sync_merge(proj, subject="合成：Merge main into staging"):
    """main 上多一个提交（发版），再把 main 回合进 staging（--no-ff 的 merge 提交）→ merge 提交的 sha。"""
    push_main(proj.root)
    _git(proj, "checkout", "-q", "-b", "mainline")
    _git(proj, "commit", "-q", "--allow-empty", "-m", "合成：main 上的发版提交")
    _git(proj, "push", "-q", "origin", "HEAD:main")
    _git(proj, "checkout", "-q", "staging")
    _git(proj, "merge", "-q", "--no-ff", "-m", subject, "mainline")
    _git(proj, "push", "-q", "origin", "HEAD:staging")
    return _git(proj, "rev-parse", "HEAD")


def test_sync_merge_from_main_does_not_block_router_ship(proj):
    merge = _sync_merge(proj)
    staging_commit(proj.root, "合成：业务票改动 (#42)")
    seed(proj, T2=_biz_ready())
    out = proj.out("prod-batch")
    assert out["router_ships"] == ["T2"] and out["queue"] is None and out["commits"] == []
    assert [c["sha"] for c in out["sync_merges"]] == [merge]
    assert out["staging_sha"] == _git(proj, "rev-parse", "origin/staging")


def test_sync_merge_plus_unattributed_commit_still_blocks(proj):
    merge = _sync_merge(proj)
    staging_commit(proj.root, "合成：业务票改动 (#42)")
    other = staging_commit(proj.root, "合成：同事的改动")
    seed(proj, T2=_biz_ready())
    out = proj.out("prod-batch")
    assert out["router_ships"] == [] and out["queue"]
    assert [c["sha"] for c in out["commits"]] == [other] and [c["sha"] for c in out["sync_merges"]] == [merge]


def test_prod_batch_reports_staging_sha(proj):
    push_main(proj.root)
    sha = staging_commit(proj.root, "合成：业务票改动 (#42)")
    seed(proj, T2=_biz_ready())
    out = proj.out("prod-batch")
    assert out["staging_sha"] == sha and out["sync_merges"] == []


def _parked_batch(proj):
    """T1 老票（走查前被停）、T4 老票（被接管）、T3 业务票（关前已撤回）的改动都还在 staging；T2 业务票可上。"""
    push_main(proj.root)
    seed(proj, T1=_legacy_merged(history=[{"at": "x", "status": "merged_staging"}]),
         T4=_legacy_merged(merge_sha="ddd", pr=44, history=[{"at": "x", "status": "merged_staging"}]),
         T3=("await_prod", [], {"kind": "business", "authorized": True, "title": "业务票三", "attempts": [],
                                "merge_sha": "ccc", "pr": 43, "history": list(H_AP)}),
         T2=_biz_ready())
    proj.out("stop", "T1")
    proj.out("takeover", "T4")
    proj.out("close", "T3", "--rolled-back")


def test_parked_members_listed_but_not_router_ships(proj):
    _parked_batch(proj)
    out = proj.out("prod-batch")
    assert out["router_ships"] == [] and out["queue"]
    st = {b["ticket"]: (b["status"], b["state"]) for b in out["batch"]}
    assert st["T1"][0] == "paused" and "已停" in st["T1"][1]
    assert st["T4"][0] == "takeover" and "接管" in st["T4"][1]
    assert st["T3"][0] == "done" and "关前已撤回" in st["T3"][1]
    (q,) = [q for q in proj.state()["queue"] if q["kind"] == "prod"]
    assert "已停" in q["summary"] and "接管" in q["summary"] and "关前已撤回" in q["summary"]
    proj.out("answer", out["queue"], "放行")                                   # 用户放行整批：它们跟着上，但不进 router_ships
    out = proj.out("prod-batch")
    assert out["router_ships"] == ["T2"] and out["queue"] is None
    assert {b["ticket"] for b in out["batch"]} == {"T1", "T2", "T3", "T4"}


def test_released_parks_paused_takeover_and_skips_rolled_back(proj):
    _parked_batch(proj)
    proj.out("answer", proj.out("prod-batch")["queue"], "放行")
    before = proj.state()["tickets"]
    out = proj.out("released", "--tickets", "T3", "--what", "合成：随批次上线", "--metrics", "m1")
    assert out["ok"] and [x["ticket"] for x in out["skipped"]] == ["T3"]
    out = proj.out("released", "--tickets", "T1", "T4", "--what", "合成：随批次上线", "--metrics", "m1")
    assert [x["ticket"] for x in out["parked"]] == ["T1", "T4"] and out["postlaunch"] == {}
    s = proj.state()["tickets"]
    assert [s[t]["status"] for t in ("T1", "T3", "T4")] == ["paused", "done", "takeover"]
    assert s["T3"]["attempts"] == before["T3"]["attempts"] == []
    for t in ("T1", "T3", "T4"):
        assert "postlaunch" not in s[t] or s[t]["postlaunch"] == before[t].get("postlaunch")
        assert s[t]["history"][-1].get("shipped_at") and "prod_ok" not in s[t]
    proj.out("released", "--tickets", "T2", "--what", "合成：业务票上线", "--metrics", "m1", "--rollback", "合成：关开关")
    assert proj.out("prod-batch").get("batch") is None                        # 全部出批次：没有业务票等上 prod


def test_released_paused_business_records_shipped_only(proj):
    push_main(proj.root)
    seed(proj, T5=("await_prod", [], {"kind": "business", "authorized": True, "title": "业务票五", "attempts": [],
                                      "merge_sha": "eee", "pr": 45, "history": list(H_AP)}))
    proj.out("stop", "T5")
    out = proj.out("released", "--tickets", "T5", "--what", "合成：随批次上线", "--metrics", "m1")   # 不要 --rollback：不记尝试
    t = proj.state()["tickets"]["T5"]
    assert [x["ticket"] for x in out["parked"]] == ["T5"]
    assert t["status"] == "paused" and t["attempts"] == [] and t["history"][-1].get("shipped_at")
