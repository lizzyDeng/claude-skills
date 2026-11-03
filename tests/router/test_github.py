import json

import pytest
from conftest import FIX, FakeCtx

from github import (GuardEmpty, LeakBlocked, add_blocked_by, comment, create_ticket_issue, ensure_map,
                    leak_hits, load_guard, merge_if_green, pr_for_branch, pr_status)

R = "o/r"
TICKET = {"id": "T3", "title": "女用户被当成男性",
          "why": {"metric": "misread", "users": "8/30", "evidence": "~/.her-persona-daily/runs/2026-10-26/summary.json"},
          "problem": "EN 链路默认用户是男性", "problem_anchor": "prompt_builder.rs:691",
          "solution": {"kind": "clue", "text": "代词槽缺省 him", "source": "router"}}


def pr_fake(fx):
    return FakeCtx().script(["gh", "pr", "view"], (FIX / fx).read_text())


def gh_issue_fake():
    f = FakeCtx()
    f.script(["gh", "issue", "create"], "https://github.com/o/r/issues/77\n")
    f.script(["gh", "api", "repos/o/r/issues/77", "--jq", ".id"], "7700\n")
    f.script(["gh", "api", "repos/o/r/issues/5", "--jq", ".id"], "500\n")
    return f


def test_guard_uids_from_users_json(tmp_path):
    (tmp_path / "w1").mkdir()
    (tmp_path / "w1" / "users.json").write_text(json.dumps({"5eed0001": {}, "ABCDEF12": {}, "short": {}, "zz99zz99": {}}))
    assert load_guard([str(tmp_path / "*" / "users.json")]).uids == {"5eed0001", "abcdef12"}
    with pytest.raises(GuardEmpty):                     # 闸失效必须报错（fail-closed），不许吞成空集合
        load_guard([str(tmp_path / "nope" / "*.json")])


def test_leak_hits_case_insensitive():
    assert leak_hits("见 5EED0001 与 abcdef12", {"5eed0001", "abcdef12", "11111111"}) == ["5eed0001", "abcdef12"]


def test_create_issue_refuses_body_with_known_uid(fake):
    t = {**TICKET, "why": {**TICKET["why"], "evidence": "见 5eed0001"}}
    with pytest.raises(LeakBlocked) as e: create_ticket_issue(fake, R, 1, t, {"5eed0001"})
    assert e.value.hits == ["5eed0001"]; assert not fake.calls_with("issue", "create")


def test_create_issue_body_has_three_sections_and_links_map():
    f = gh_issue_fake()
    assert create_ticket_issue(f, R, 5, TICKET, {"5eed0001"}) == 77
    create = f.calls_with("issue", "create")[0]
    body = create[create.index("--body") + 1]
    for part in ("Part of #5", "为什么派", "解决什么问题", "router 线索（非方案）", "prompt_builder.rs:691"):
        assert part in body
    assert create[create.index("--title") + 1] == "女用户被当成男性"


def test_sub_issue_and_blocked_by_use_database_ids():
    f = gh_issue_fake()
    create_ticket_issue(f, R, 5, TICKET, set())
    assert ["gh", "api", "--method", "POST", "repos/o/r/issues/5/sub_issues", "-F", "sub_issue_id=7700"] in f.calls
    add_blocked_by(f, R, 77, 5)
    assert f.last_call() == ["gh", "api", "--method", "POST", "repos/o/r/issues/77/dependencies/blocked_by", "-F", "issue_id=500"]


def test_ensure_map_reuses_existing():
    f = FakeCtx().script(["gh", "issue", "list"], json.dumps([{"number": 3, "title": "her 周报问题（旧）"},
                                                              {"number": 9, "title": "her 周报问题"}]))
    assert ensure_map(f, R, "her 周报问题") == 9 and not f.calls_with("issue", "create")
    g = FakeCtx().script(["gh", "issue", "list"], "[]").script(["gh", "issue", "create"], "https://github.com/o/r/issues/12\n")
    assert ensure_map(g, R, "her 周报问题") == 12
    c = g.calls_with("issue", "create")[0]; assert c[c.index("--label") + 1] == "wayfinder:map"


def test_pr_status_classifies_checks():
    assert pr_status(pr_fake("gh_pr_green.json"), R, 9)["checks"] == "success"
    assert pr_status(pr_fake("gh_pr_pending.json"), R, 9)["checks"] == "pending"
    assert pr_status(pr_fake("gh_pr_no_checks.json"), R, 9)["checks"] == "none"
    assert pr_status(pr_fake("gh_pr_failed.json"), R, 9)["checks"] == "failure"
    m = pr_status(pr_fake("gh_pr_merged.json"), R, 9)
    assert m["merged"] is True and m["merge_sha"] == "deadbeef"


def test_merge_refuses_pending_or_empty_checks():
    for fx in ("gh_pr_pending.json", "gh_pr_no_checks.json", "gh_pr_failed.json"):
        f = pr_fake(fx)
        assert merge_if_green(f, R, 9) is False and not f.calls_with("pr", "merge")
    f = pr_fake("gh_pr_green.json")
    assert merge_if_green(f, R, 9) is True
    # 不带 --auto；锁死验过 CI 的那个 head，防止验完之后又有人推了新提交
    assert f.last_call() == ["gh", "pr", "merge", "9", "--squash", "--match-head-commit", "abc123", "-R", "o/r"]


def test_merge_reports_already_merged_and_failed_merge():
    f = pr_fake("gh_pr_merged.json")
    assert merge_if_green(f, R, 9) is True and not f.calls_with("pr", "merge")
    g = pr_fake("gh_pr_green.json").script(["gh", "pr", "merge"], "", returncode=1, stderr="head changed")
    assert merge_if_green(g, R, 9) is False


def test_pr_for_branch():
    f = FakeCtx().script(["gh", "pr", "list"], json.dumps([{"number": 41}]))
    assert pr_for_branch(f, R, "router/T3") == 41
    assert pr_for_branch(FakeCtx().script(["gh", "pr", "list"], "[]"), R, "router/T3") is None


def test_comment_is_leak_guarded(fake):
    with pytest.raises(LeakBlocked): comment(fake, R, 7, "合了，影响 5eed0001", {"5eed0001"})
    comment(fake, R, 7, "合了 abc123", {"5eed0001"})
    assert fake.last_call()[:4] == ["gh", "issue", "comment", "7"]


# ---- 终审修复 ----------------------------------------------------------------
from github import GuardEmpty, load_guard  # noqa: E402


def test_merge_refuses_wrong_base():                                   # C1：漏写 --base 的 PR 不能被合进 main
    f = pr_fake("gh_pr_wrong_base.json")
    assert pr_status(f, R, 9)["base"] == "main"
    assert merge_if_green(f, R, 9, base="staging") is False and not f.calls_with("pr", "merge")
    assert merge_if_green(pr_fake("gh_pr_green.json"), R, 9, base="staging") is True


def test_pr_for_branch_filters_base():
    f = FakeCtx().script(["gh", "pr", "list"], json.dumps([{"number": 41}]))
    pr_for_branch(f, R, "router/T3", base="staging")
    c = f.last_call(); assert c[c.index("--base") + 1] == "staging"


def test_all_skipped_checks_do_not_count_as_green():
    assert pr_status(pr_fake("gh_pr_all_skipped.json"), R, 9)["checks"] == "none"


def test_pr_status_reports_mergeable():
    assert pr_status(pr_fake("gh_pr_conflict.json"), R, 9)["mergeable"] == "CONFLICTING"


def test_guard_collects_nested_uid_values_and_quotes(tmp_path):        # C4
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "persona.json").write_text(json.dumps(
        {"quotes": {"S1": {"uid": "a1b2c3d4", "quote": "The purple elephant reads poems on mondays"}},
         "q1": [{"text": "do you remember what I told you yesterday"}], "n": "20261026"}))
    (tmp_path / "w" / "labels_0.jsonl").write_text(json.dumps({"uid": "0f0f0f0f", "x": 1}) + "\n" + "not json\n")
    g = load_guard([str(tmp_path / "w" / "*.json*")])
    assert {"a1b2c3d4", "0f0f0f0f"} <= g.uids and "20261026" not in g.uids
    assert leak_hits("证据：A1B2C3D4", g) == ["a1b2c3d4"]
    hits = leak_hits("用户说 the purple  elephant reads poems on mondays 所以", g)
    assert hits and hits[0].startswith("原话")
    assert leak_hits("prompt 写着 You're a real person, not an AI.", g) == []


def test_guard_fails_closed_when_globs_match_nothing(tmp_path):
    with pytest.raises(GuardEmpty): load_guard([str(tmp_path / "nope" / "*.json")])
    assert load_guard([]).uids == set()


# ---- plan v4：R1 改指判定、R6 逐目标幂等留言 / 关 issue -------------------------
import github  # noqa: E402


def _timeline(*nodes):
    """gh api graphql 的完整响应（不带 --jq）。"""
    return json.dumps({"data": {"repository": {"pullRequest": {"timelineItems": {"nodes": list(nodes)}}}}})


AUTO = {"__typename": "AutomaticBaseChangeSucceededEvent", "oldBase": "staging", "newBase": "main",
        "createdAt": "2026-11-02T13:20:00Z"}
HUMAN = {"__typename": "BaseRefChangedEvent", "previousRefName": "staging", "currentRefName": "main",
         "createdAt": "2026-11-02T13:30:00Z", "actor": {"login": "someone"}}


def test_auto_retargeted_reads_latest_base_change_event():
    gql = lambda out, rc=0: FakeCtx().script(["gh", "api", "graphql"], out, returncode=rc)
    f = gql(_timeline(HUMAN, AUTO))                                   # 最近一条是 GitHub 自动改指
    assert github.auto_retargeted(f, R, 41, "staging") is True
    assert f.calls_with("graphql") and f.timeout_of("graphql") is not None
    assert github.auto_retargeted(gql(_timeline(AUTO, HUMAN)), R, 41, "staging") is False   # 之后人手改过
    assert github.auto_retargeted(gql(_timeline()), R, 41, "staging") is False             # 没有改指事件
    other = {**AUTO, "oldBase": "develop"}
    assert github.auto_retargeted(gql(_timeline(other)), R, 41, "staging") is False         # 不是从我们的 base 改走的
    assert github.auto_retargeted(gql("", rc=1), R, 41, "staging") is None                 # GhError
    assert github.auto_retargeted(gql("{not json"), R, 41, "staging") is None              # 坏 JSON


def test_auto_retargeted_pr_is_moved_back_with_pr_edit():
    f = FakeCtx()
    github.retarget_pr(f, R, 41, "staging")
    assert f.last_call() == ["gh", "pr", "edit", "41", "-R", "o/r", "--base", "staging"]
    with pytest.raises(github.GhError):
        github.retarget_pr(FakeCtx().script(["gh", "pr", "edit"], "", returncode=1, stderr="x"), R, 41, "staging")


def _view(state="OPEN", *bodies):
    return json.dumps({"state": state, "comments": [{"body": b} for b in bodies]})


MARK = "<!-- router:downstream:T1->T3 -->"
LEGACY = r"上游 T1(?: #\d+)?「"
OLD_BODY = ("上游 T1 #944「周报口径」已完成（合进 staging），SHA `仓外改动，无 commit`。\n- 合了什么：x\n"
            "- 对本票的影响：共享代码面 weekly-report 已释放；开工 / 继续前先基于最新 staging。\n\n由 /router 自动留言。")


def test_comment_once_recognizes_marker_and_legacy_signature():
    def run(view_out, body="上游 T1「周报口径」已完成（仓外改动，不经 PR）", guard=()):
        f = FakeCtx().script(["gh", "issue", "view", "946"], view_out)
        return f, github.comment_once(f, R, 946, body, guard, MARK, legacy=LEGACY)
    f, res = run(_view("OPEN", "无关评论", f"早先的留言\n\n{MARK}"))          # marker 命中 ⇒ 跳过
    assert "already" in str(res) and not f.calls_with("issue", "comment")
    assert ["gh", "issue", "view", "946", "-R", "o/r", "--json", "state,comments"] == f.calls[0]
    f, res = run(_view("OPEN", OLD_BODY))                                     # 旧格式签名 ⇒ 跳过
    assert "already" in str(res) and not f.calls_with("issue", "comment")
    f, res = run(_view("OPEN", OLD_BODY.replace("上游 T1 #944", "上游 T10 #950")))   # T10 不是 T1
    assert "commented" in str(res)
    c = f.calls_with("issue", "comment")[0]
    assert c[:5] == ["gh", "issue", "comment", "946", "-R"] and c[c.index("--body") + 1].endswith(MARK)
    f, res = run(_view("OPEN", "> " + OLD_BODY))                              # 旧格式出现在引用块里：不算
    assert "commented" in str(res)
    f, res = run(_view("OPEN", "上游 T1 #944「周报口径」随手写的，不是 router 的留言"))   # 没有签名尾巴：不算
    assert "commented" in str(res)
    with pytest.raises(LeakBlocked):                                          # 被防泄漏闸挡 ⇒ 不调 comment
        f = FakeCtx().script(["gh", "issue", "view", "946"], _view("OPEN"))
        try:
            github.comment_once(f, R, 946, "影响 5eed0001", {"5eed0001"}, MARK, legacy=LEGACY)
        finally:
            assert not f.calls_with("issue", "comment")


def test_close_issue_comments_once_by_marker_and_guards_body():
    mark = "<!-- router:closeout:T1 -->"
    body = "仓外改动已由你核对（2026-11-03），本票完成。由 /router 关闭。"
    f = FakeCtx().script(["gh", "issue", "view", "944"], _view("OPEN"))
    github.close_issue(f, R, 944, body, (), mark)
    assert f.calls_with("issue", "comment") and f.calls_with("issue", "close")
    assert f.calls.index(f.calls_with("issue", "comment")[0]) < f.calls.index(f.calls_with("issue", "close")[0])
    assert ["gh", "issue", "close", "944", "-R", "o/r", "--reason", "completed"] in f.calls
    assert not any("--comment" in c for c in f.calls_with("issue", "close"))    # 不用 close --comment（内部是两次调用）
    f = FakeCtx().script(["gh", "issue", "view", "944"], _view("OPEN", f"{body}\n\n{mark}"))   # 上次留言成功、关失败
    github.close_issue(f, R, 944, body, (), mark)
    assert not f.calls_with("issue", "comment") and f.calls_with("issue", "close")
    f = FakeCtx().script(["gh", "issue", "view", "944"], _view("CLOSED", f"{body}\n\n{mark}"))  # 已经关了
    github.close_issue(f, R, 944, body, (), mark)
    assert not f.calls_with("issue", "comment") and not f.calls_with("issue", "close")
    f = FakeCtx().script(["gh", "issue", "view", "944"], _view("OPEN"))
    with pytest.raises(LeakBlocked):
        github.close_issue(f, R, 944, "影响 5eed0001", {"5eed0001"}, mark)
    assert not f.calls_with("issue", "comment") and not f.calls_with("issue", "close")


# ---- FX：终审后修复轮 ------------------------------------------------------------------------

@pytest.mark.parametrize("base", ["main", "master", "prod"])
def test_merge_never_targets_main_even_if_config_says_so(base):          # F10（RV M-7）
    f = pr_fake("gh_pr_green.json")
    assert merge_if_green(f, R, 9, base=base, main="prod") is False      # 配置写错成 main / master / 仓库默认分支
    assert f.calls == []                                                  # 连 gh 都不调
    h = pr_fake("gh_pr_wrong_base.json")                                  # PR 指向 main、配置也写成 main：照样不合
    assert merge_if_green(h, R, 9, base="main") is False and not h.calls_with("pr", "merge")
    g = pr_fake("gh_pr_wrong_base.json")                                  # PR 本身指向 main：不传 base 也不合
    assert merge_if_green(g, R, 9) is False and not g.calls_with("pr", "merge")
