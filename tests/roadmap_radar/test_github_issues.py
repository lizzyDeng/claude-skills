import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2] / "skills" / "roadmap-radar" / "scripts"
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(BASE))

import model  # noqa: E402
from sources import github_issues  # noqa: E402

REPO = "hyoteam/aifriends"


class FakeCtx:
    """录制回放：把 gh 调用按 args 特征映射到 fixture。"""

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
        if "/sub_issues" in joined:
            number = joined.rsplit("/issues/", 1)[1].split("/")[0]
            path = FIX / ("gh_sub_issues_%s.json" % number)
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        if "/issues/" in joined:
            number = int(joined.rsplit("/issues/", 1)[1])
            return {"number": number,
                    "issue_dependencies_summary": {
                        "blocked_by": self.deps.get(number, 0)}}
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
    assert "frontier" in nodes["gh:%s#13" % REPO]["badges"]        # open + 无 assignee
    assert "frontier" not in nodes["gh:%s#22" % REPO]["badges"]    # 有 assignee
    assert "claimed:lizzydeng" in nodes["gh:%s#22" % REPO]["badges"]
    assert "frontier" not in nodes["gh:%s#25" % REPO]["badges"]    # 已关


def test_blocked_issue_is_not_frontier():
    ctx = FakeCtx(deps={13: 1})
    nodes = {n["id"]: n for n in collect(ctx=ctx, fetch_dependencies=True)}
    assert "blocked" in nodes["gh:%s#13" % REPO]["badges"]
    assert "frontier" not in nodes["gh:%s#13" % REPO]["badges"]


def test_dependencies_not_fetched_by_default_keeps_call_count_low():
    ctx = FakeCtx()
    collect(ctx=ctx)
    assert not any("/issues/" in " ".join(a) and "/sub_issues" not in " ".join(a)
                   for a in ctx.calls)


def test_dependency_fetch_only_targets_open_children_of_groups():
    ctx = FakeCtx()
    collect(ctx=ctx, fetch_dependencies=True)
    probed = [a for a in ctx.calls
              if "/issues/" in " ".join(a) and "/sub_issues" not in " ".join(a)]
    numbers = {int(" ".join(a).rsplit("/issues/", 1)[1]) for a in probed}
    assert numbers == {13}   # 22/16 不是 map 的子票；25 已关；99 无 goal


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


def test_unknown_goal_from_form_raises():
    try:
        collect(goal_from="magic:xyz")
    except ValueError as exc:
        assert "magic" in str(exc)
    else:
        raise AssertionError("expected ValueError")
