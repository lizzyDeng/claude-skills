import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2] / "skills" / "roadmap-radar" / "scripts"
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(BASE))

import model  # noqa: E402
from sources import github_issues  # noqa: E402

REPO = "hyoteam/aifriends"


SAME_REPO_URL = "https://api.github.com/repos/hyoteam/aifriends"


def dep(number, state="open", repo_url=SAME_REPO_URL):
    return {"number": number, "state": state, "repository_url": repo_url}


class FakeCtx:
    """录制回放：把 gh 调用按 args 特征映射到 fixture。

    deps = {票号: [dep(...), ...]} —— /dependencies/blocked_by 的回放数据。
    """

    def __init__(self, deps=None):
        self.deps = deps or {}
        self.calls = []

    def resolve(self, rel):
        return rel

    def gh_json(self, args):
        self.calls.append(args)
        joined = " ".join(args)
        if "issue" in args and "list" in args:
            return json.loads((FIX / "gh_issues.json").read_text(encoding="utf-8"))
        if "graphql" in args:
            return json.loads(
                (FIX / "gh_graphql_sub_issues.json").read_text(encoding="utf-8"))
        if "/dependencies/blocked_by" in joined:
            number = int(joined.rsplit("/issues/", 1)[1].split("/")[0])
            return self.deps.get(number, [])
        raise AssertionError("unexpected gh call: %r" % (args,))


def collect(ctx=None, **cfg):
    cfg.setdefault("repo", REPO)
    cfg.setdefault("goal_from", "label:^obj-\\d+$")
    cfg.setdefault("group_from", "label:^wayfinder:map$")
    return github_issues.collect(dict(cfg, type="github-issues"), ctx or FakeCtx())


def test_label_becomes_goal_node_with_canonical_id():
    nodes = {n["id"]: n for n in collect()}
    assert nodes["goal:obj-2"]["kind"] == model.KIND_GOAL
    assert nodes["goal:obj-3"]["kind"] == model.KIND_GOAL


def test_map_label_becomes_group_parented_to_its_goal():
    nodes = {n["id"]: n for n in collect()}
    map12 = nodes["gh:%s#12" % REPO]
    assert map12["kind"] == model.KIND_GROUP
    assert map12["parent"] == "goal:obj-2"
    assert map12["url"].endswith("/issues/12")


def test_sub_issues_become_leaves_under_the_map():
    nodes = {n["id"]: n for n in collect()}
    assert nodes["gh:%s#13" % REPO]["parent"] == "gh:%s#12" % REPO
    assert nodes["gh:%s#25" % REPO]["parent"] == "gh:%s#12" % REPO


def test_issue_state_maps_to_binary_progress():
    nodes = {n["id"]: n for n in collect()}
    assert nodes["gh:%s#25" % REPO]["progress"] == 1.0   # CLOSED
    assert nodes["gh:%s#13" % REPO]["progress"] == 0.0   # OPEN


def test_frontier_badge_needs_open_unassigned_unblocked():
    nodes = {n["id"]: n for n in collect()}
    assert "frontier" in nodes["gh:%s#30" % REPO]["badges"]        # open + 无 assignee
    assert "frontier" not in nodes["gh:%s#22" % REPO]["badges"]    # 有 assignee
    assert "claimed:lizzydeng" in nodes["gh:%s#22" % REPO]["badges"]
    assert "frontier" not in nodes["gh:%s#25" % REPO]["badges"]    # 已关


def test_issue_with_sub_issues_is_not_frontier():
    """#13 自己还有子票 #30，不是可直接上手的票 —— frontier 给 #30。"""
    nodes = {n["id"]: n for n in collect()}
    assert "frontier" not in nodes["gh:%s#13" % REPO]["badges"]


def test_sub_issue_of_non_map_issue_chains_to_upstream_issue():
    """子票挂在上游 issue（非 map）下也要建边：12 → 13 → 30。"""
    nodes = {n["id"]: n for n in collect()}
    assert nodes["gh:%s#30" % REPO]["parent"] == "gh:%s#13" % REPO
    assert nodes["gh:%s#13" % REPO]["parent"] == "gh:%s#12" % REPO


def test_cross_repo_sub_issues_are_filtered_out():
    """#14 的子票在别的仓 —— 不能拿 number 撞本仓的票乱挂。"""
    nodes = collect()
    map14 = "gh:%s#14" % REPO
    assert not [n for n in nodes if n["parent"] == map14]


def test_open_blocker_marks_blocked_and_kills_frontier():
    ctx = FakeCtx(deps={30: [dep(16, "open")]})
    nodes = {n["id"]: n for n in collect(ctx=ctx, fetch_dependencies=True)}
    node = nodes["gh:%s#30" % REPO]
    assert node["meta"]["blocked_by"] == ["gh:%s#16" % REPO]
    assert "blocked" in node["badges"]
    assert "frontier" not in node["badges"]


def test_closed_blockers_keep_edge_but_not_blocked():
    """前置全关 = 链条历史仍在（画绿点），但票可上手。"""
    ctx = FakeCtx(deps={30: [dep(25, "closed")]})
    nodes = {n["id"]: n for n in collect(ctx=ctx, fetch_dependencies=True)}
    node = nodes["gh:%s#30" % REPO]
    assert node["meta"]["blocked_by"] == ["gh:%s#25" % REPO]
    assert "blocked" not in node["badges"]
    assert "frontier" in node["badges"]


def test_cross_repo_blockers_are_filtered_out():
    ctx = FakeCtx(deps={30: [dep(30, "open",
                               "https://api.github.com/repos/other/repo")]})
    nodes = {n["id"]: n for n in collect(ctx=ctx, fetch_dependencies=True)}
    assert "blocked_by" not in nodes["gh:%s#30" % REPO]["meta"]


def test_dependencies_not_fetched_by_default_keeps_call_count_low():
    ctx = FakeCtx()
    collect(ctx=ctx)
    assert not any("/dependencies/" in " ".join(a) for a in ctx.calls)


def test_dependency_fetch_covers_every_issue_including_closed():
    """已关票的前置也要拉 —— 链条历史不完整会看不懂一张票为什么能动。"""
    ctx = FakeCtx()
    collect(ctx=ctx, fetch_dependencies=True)
    probed = [a for a in ctx.calls if "/dependencies/blocked_by" in " ".join(a)]
    numbers = {int(" ".join(a).rsplit("/issues/", 1)[1].split("/")[0])
               for a in probed}
    assert numbers == {12, 13, 14, 16, 22, 25, 30, 99}


def test_issue_without_goal_or_parent_is_kept_not_dropped():
    nodes = {n["id"]: n for n in collect()}
    assert "gh:%s#99" % REPO in nodes
    assert nodes["gh:%s#99" % REPO]["parent"] is None


def test_orphan_wayfinder_ticket_kept_with_no_parent():
    """#16 有 wayfinder:task 但不是任何 map 的子票 —— 必须留在图里。"""
    nodes = {n["id"]: n for n in collect()}
    assert nodes["gh:%s#16" % REPO]["parent"] is None


def test_milestone_goal_source():
    class MilestoneCtx(FakeCtx):
        def gh_json(self, args):
            if "issue" in args and "list" in args:
                return [{"number": 1, "title": "T", "state": "OPEN", "url": "u",
                         "body": "", "labels": [], "assignees": [],
                         "milestone": {"title": "Q3 变现"}}]
            return super().gh_json(args)

    nodes = {n["id"]: n for n in collect(ctx=MilestoneCtx(), goal_from="milestone",
                                        group_from=None)}
    assert nodes["goal:Q3 变现"]["kind"] == model.KIND_GOAL
    assert nodes["gh:%s#1" % REPO]["parent"] == "goal:Q3 变现"


def test_summary_is_first_content_line_skipping_markdown_noise():
    body = ("<!-- template -->\n## 目标\n![img](x.png)\n\n"
            "- **让宿主 App** 能[接入](u) SDK\n第二行不要")
    assert github_issues.summary_of(body) == "让宿主 App 能接入 SDK"


def test_summary_truncates_long_lines():
    out = github_issues.summary_of("x" * 300)
    assert len(out) == github_issues.SUMMARY_MAX
    assert out.endswith("…")


def test_summary_of_empty_body_is_none():
    assert github_issues.summary_of("") is None
    assert github_issues.summary_of(None) is None


def test_collect_puts_summary_into_meta():
    nodes = {n["id"]: n for n in collect()}
    assert nodes["gh:%s#12" % REPO]["meta"]["summary"] == "4 个借鉴点全部定型"
    assert nodes["gh:%s#13" % REPO]["meta"]["summary"] == "分层阈值怎么定"


def test_unknown_goal_from_form_raises():
    try:
        collect(goal_from="magic:xyz")
    except ValueError as exc:
        assert "magic" in str(exc)
    else:
        raise AssertionError("expected ValueError")
