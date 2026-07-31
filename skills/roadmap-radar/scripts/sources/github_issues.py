"""通用 GitHub source。任何仓库都能用，不含任何 wayfinder/forge 知识。

goal 从 label 正则或 milestone 来；层级从 GitHub sub-issues 来 ——
一次分页 GraphQL 拉全仓 sub-issue 边，所以子票挂在上游 issue 下（任意深度）
也能建出父子链，不只挂在 map 下。
先后关系 = GitHub native blocking：fetch_dependencies 开着时逐票
（含已关的，链条历史要完整）拉 /dependencies/blocked_by 全列表，
存进 meta.blocked_by（node id 列表，同仓过滤）。
frontier = open + 无 assignee + 自己没子票 + 所有前置 blocker 都已关。
"""

import re

import model

ISSUE_FIELDS = "number,title,state,labels,assignees,url,body,milestone"
DEFAULT_LIMIT = 300

SUMMARY_MAX = 120
PRIORITY_RE = re.compile(r"^[Pp](\d)$")

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
# 整行都是结构噪音：标题、图片、代码围栏、表格行、分隔线
_NOISE_LINE_RE = re.compile(r"^(#{1,6}\s|!\[|```|\||-{3,}\s*$)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARK_RE = re.compile(r"[*_`~]")


def summary_of(body):
    """issue 正文 -> 一句话简介：第一个有内容的行，去 markdown 记号。

    没有可用行返回 None（渲染层不显示，而不是显示空串）。
    """
    text = _HTML_COMMENT_RE.sub("", body or "")
    for line in text.splitlines():
        line = line.strip()
        if not line or _NOISE_LINE_RE.match(line):
            continue
        line = re.sub(r"^[-*>]\s+", "", line)
        line = _LINK_RE.sub(r"\1", line)
        line = _MARK_RE.sub("", line).strip()
        if line:
            return line if len(line) <= SUMMARY_MAX else line[:SUMMARY_MAX - 1] + "…"
    return None


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
                  "summary": summary_of(issue.get("body")),
                  "priority": _priority_of(issue),
                  "labels": [label["name"] for label in issue.get("labels", [])]},
        )
        nodes.append(node)
        by_number[issue["number"]] = node

    # 层级：GitHub sub-issues。一次分页 GraphQL 拉全仓的边，任意深度都在。
    edges = _sub_issue_edges(ctx, repo)
    child_numbers = set()
    for parent_number, children in edges.items():
        parent_node = by_number.get(parent_number)
        if parent_node is None:
            continue
        for child_number in children:
            target = by_number.get(child_number)
            if target is None:
                continue
            target["parent"] = parent_node["id"]
            child_numbers.add(child_number)

    # 先后边：native blocking，逐票拉全列表（含已关票 —— 链条历史要完整）
    blocked = set()
    if fetch_deps:
        for number in sorted(by_number):
            node = by_number[number]
            rows = ctx.gh_json([
                "api", "repos/%s/issues/%s/dependencies/blocked_by" % (repo, number)
            ]) or []
            blockers = [r for r in rows
                        if (r.get("repository_url") or "/repos/%s" % repo)
                        .endswith("/repos/%s" % repo)]
            if not blockers:
                continue
            node["meta"]["blocked_by"] = [nid(r["number"]) for r in blockers]
            if node["state"] == "open" and any(
                    str(r.get("state", "")).lower() == "open" for r in blockers):
                blocked.add(number)
                node["badges"].append("blocked")

    for number, node in by_number.items():
        if (node["kind"] == model.KIND_LEAF and node["state"] == "open"
                and not any(b.startswith("claimed:") for b in node["badges"])
                and number not in blocked
                and number not in edges):     # 有子票的票不是可直接上手的票
            node["badges"].append("frontier")

    return list(goals.values()) + nodes


_SUB_ISSUES_QUERY = """
query($owner:String!,$name:String!,$cursor:String){
  repository(owner:$owner,name:$name){
    issues(first:100,after:$cursor,states:[OPEN,CLOSED]){
      pageInfo{hasNextPage endCursor}
      nodes{number subIssues(first:100){
        nodes{number repository{nameWithOwner}}}}
    }
  }
}""".strip()


def _sub_issue_edges(ctx, repo):
    """{parent_number: [child_number, ...]}，只保留同仓子票（跨仓边挂不上，丢会误导）。"""
    owner, name = repo.split("/", 1)
    edges = {}
    cursor = None
    while True:
        args = ["api", "graphql", "-f", "query=%s" % _SUB_ISSUES_QUERY,
                "-F", "owner=%s" % owner, "-F", "name=%s" % name]
        if cursor:
            args += ["-F", "cursor=%s" % cursor]
        data = ctx.gh_json(args) or {}
        block = (((data.get("data") or {}).get("repository") or {})
                 .get("issues") or {})
        for row in block.get("nodes") or []:
            children = [c["number"]
                        for c in (row.get("subIssues") or {}).get("nodes") or []
                        if (c.get("repository") or {}).get("nameWithOwner",
                                                           repo) == repo]
            if children:
                edges[row["number"]] = children
        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return edges
        cursor = page.get("endCursor")


def _priority_of(issue):
    """P0/P1/… label -> int。没打返回 None（渲染层排最后、不显示徽标）。"""
    for label in issue.get("labels", []):
        match = PRIORITY_RE.match(label["name"])
        if match:
            return int(match.group(1))
    return None


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
