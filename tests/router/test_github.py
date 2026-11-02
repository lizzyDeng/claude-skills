import json

import pytest
from conftest import FIX, FakeCtx

from github import (LeakBlocked, add_blocked_by, comment, create_ticket_issue, ensure_map,
                    leak_hits, load_guard_uids, merge_if_green, pr_for_branch, pr_status)

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
    assert load_guard_uids([str(tmp_path / "*" / "users.json")]) == {"5eed0001", "abcdef12"}
    assert load_guard_uids([str(tmp_path / "nope" / "*.json")]) == set()


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
