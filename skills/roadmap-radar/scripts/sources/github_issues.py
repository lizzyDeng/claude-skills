"""通用 GitHub source。任何仓库都能用，不含任何 wayfinder/forge 知识。

goal 从 label 正则或 milestone 来；层级从 GitHub sub-issues 来；
frontier = open + 无 assignee + 无 open blocker。
"""

import re

import model

ISSUE_FIELDS = "number,title,state,labels,assignees,url,body,milestone"
DEFAULT_LIMIT = 300


def collect(config, ctx):
    repo = config["repo"]
    limit = config.get("limit", DEFAULT_LIMIT)
    goal_from = config.get("goal_from") or "label:^obj-\\d+$"
    group_from = config.get("group_from")
    fetch_deps = bool(config.get("fetch_dependencies", False))

    issues = ctx.gh_json([
        "issue", "list", "--repo", repo, "--state", "all",
        "--limit", str(limit), "--json", ISSUE_FIELDS,
    ])

    nid = lambda number: "gh:%s#%s" % (repo, number)  # noqa: E731
    group_re = re.compile(group_from.partition(":")[2]) if group_from else None

    goals = {}
    nodes = []
    by_number = {}

    for issue in issues:
        raw_goals = _goal_raws(issue, goal_from)
        for raw in raw_goals:
            if raw not in goals:
                goals[raw] = model.node(
                    id="goal:%s" % raw, kind=model.KIND_GOAL, title=raw,
                    meta={"source": "github-issues", "repo": repo})
        is_group = bool(group_re) and any(
            group_re.search(label["name"]) for label in issue.get("labels", []))
        closed = str(issue.get("state", "")).lower() == "closed"
        assignees = [a["login"] for a in issue.get("assignees") or []]

        node = model.node(
            id=nid(issue["number"]),
            kind=model.KIND_GROUP if is_group else model.KIND_LEAF,
            title=issue["title"],
            url=issue.get("url"),
            parent=("goal:%s" % raw_goals[0]) if raw_goals else None,
            progress=None if is_group else (1.0 if closed else 0.0),
            state=str(issue.get("state", "")).lower(),
            badges=["claimed:%s" % a for a in assignees],
            meta={"source": "github-issues", "repo": repo,
                  "number": issue["number"], "body": issue.get("body") or "",
                  "labels": [label["name"] for label in issue.get("labels", [])]},
        )
        nodes.append(node)
        by_number[issue["number"]] = node

    # 层级：GitHub sub-issues。只对 group 节点问，成本 = group 数量。
    child_numbers = set()
    for node in nodes:
        if node["kind"] != model.KIND_GROUP:
            continue
        for child in ctx.gh_json([
            "api", "repos/%s/issues/%s/sub_issues" % (repo, node["meta"]["number"])
        ]) or []:
            target = by_number.get(child["number"])
            if target is None:
                continue
            target["parent"] = node["id"]
            child_numbers.add(child["number"])

    # blocker：只查 group 名下的 open 子票，避免 N 次 API
    blocked = set()
    if fetch_deps:
        for number in sorted(child_numbers):
            node = by_number[number]
            if node["state"] != "open":
                continue
            detail = ctx.gh_json(["api", "repos/%s/issues/%s" % (repo, number)]) or {}
            summary = detail.get("issue_dependencies_summary") or {}
            if summary.get("blocked_by", 0) > 0:
                blocked.add(number)
                node["badges"].append("blocked")

    for number, node in by_number.items():
        if (node["kind"] == model.KIND_LEAF and node["state"] == "open"
                and not any(b.startswith("claimed:") for b in node["badges"])
                and number not in blocked):
            node["badges"].append("frontier")

    return list(goals.values()) + nodes


def _goal_raws(issue, goal_from):
    """把 goal_from 配置翻译成这张 issue 归属的 goal 原始标识列表。"""
    form, _, arg = goal_from.partition(":")
    if form == "label":
        pattern = re.compile(arg)
        return [label["name"] for label in issue.get("labels", [])
                if pattern.search(label["name"])]
    if form == "milestone":
        milestone = issue.get("milestone") or {}
        title = milestone.get("title")
        return [title] if title else []
    raise ValueError(
        "unknown goal_from form %r; use 'label:<regex>' or 'milestone'" % form)
