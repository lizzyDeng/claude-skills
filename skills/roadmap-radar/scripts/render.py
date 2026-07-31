"""Node 树 -> 单文件 HTML。域无关：只认 Node 的字段，不认业务。

每个 goal 渲成一张横向拓扑树：goal 节点在左，group/leaf 逐层向右，
连线全部纯 CSS（li 的 ::before/::after 画折线），零 JS，无 CDN，双击即开。
每个节点是一个盒：标题 + 进度 + 一句话简介（meta.summary）。
"""

import html as html_mod

import model

CSS = """
:root { color-scheme: light dark; --fg:#111; --dim:#666; --line:#ddd; --edge:#c5c5c5;
        --bar:#3b82f6; --ok:#16a34a; --warn:#dc2626; --card:#fff; --bg:#fafafa;
        --nodew:240px; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e7e7e7; --dim:#999; --line:#333; --edge:#4a4a4a;
          --card:#1a1a1a; --bg:#111; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
       "PingFang SC","Hiragino Sans GB",sans-serif; }
header { border-bottom:1px solid var(--line); padding-bottom:14px; margin-bottom:18px; }
header .ns { font-size:19px; font-weight:600; }
header .meta { color:var(--dim); font-size:12px; margin-top:4px; }
.pct { font-variant-numeric:tabular-nums; font-weight:600; float:right;
       margin-left:8px; font-size:12px; }
.bar { height:4px; background:var(--line); border-radius:2px; overflow:hidden;
       margin:6px 0 2px; }
.bar > i { display:block; height:100%; background:var(--bar); }
.bar.full > i { background:var(--ok); }
a { color:inherit; }

/* ---- 拓扑树：goal 在左，子节点逐层向右，折线连接 ---- */
.goal-sec { background:var(--card); border:1px solid var(--line); border-radius:10px;
            padding:10px 14px; margin-bottom:14px; overflow-x:auto; }
.tree { display:flex; align-items:center; width:max-content; min-width:100%;
        padding:4px 0; }
.tree ul { list-style:none; margin:0; padding:0; position:relative;
           display:flex; flex-direction:column; justify-content:center; }
.tree ul::before { content:""; position:absolute; left:0; top:50%; width:16px;
                   height:1px; background:var(--edge); }
.tree li { position:relative; display:flex; align-items:center;
           padding:3px 0 3px 32px; }
.tree li::before { content:""; position:absolute; left:16px; top:50%; width:16px;
                   height:1px; background:var(--edge); }
.tree li::after { content:""; position:absolute; left:16px; top:0; bottom:0;
                  width:1px; background:var(--edge); }
.tree li:first-child::after { top:50%; }
.tree li:last-child::after { bottom:50%; }
.tree li:only-child::after { display:none; }

/* ---- 节点盒 ---- */
.node { width:var(--nodew); flex:none; background:var(--card);
        border:1px solid var(--line); border-radius:8px; padding:7px 10px; }
.node .t { font-size:13px; }
.node.goal { border-width:2px; }
.node.goal .t { font-size:14.5px; font-weight:600; }
.node .sum { color:var(--dim); font-size:11.5px; margin-top:3px;
             display:-webkit-box; -webkit-line-clamp:2;
             -webkit-box-orient:vertical; overflow:hidden; }
.node .metric { color:var(--dim); font-size:11px; margin-top:4px;
                overflow-wrap:anywhere; }
.node.frontier-node { border-color:var(--bar);
                      box-shadow:0 0 0 1px var(--bar) inset; }
.node.warn-node { border-color:var(--warn); }
.node.done-node { opacity:.62; }
.done { color:var(--dim); text-decoration:line-through; }
.tags { margin-top:4px; }
.tag { display:inline-block; font-size:10.5px; padding:0 5px; border-radius:9px;
       border:1px solid var(--line); color:var(--dim); margin-right:4px; }
.tag.frontier { border-color:var(--bar); color:var(--bar); font-weight:600; }
.tag.warn { border-color:var(--warn); color:var(--warn); }

/* ---- 未归位区 ---- */
#unassigned { margin-top:26px; border:1px solid var(--warn); border-radius:10px;
              padding:12px 14px; overflow-x:auto; }
#unassigned h2 { color:var(--warn); font-size:15px; margin:0 0 8px; }
#unassigned ul.flat { list-style:none; margin:5px 0; padding:0; }
#unassigned ul.flat > li { margin:6px 0; }
table.dangling { border-collapse:collapse; font-size:12px; }
table.dangling td { padding:1px 10px 1px 0; }
"""


def esc(value):
    return html_mod.escape(str(value if value is not None else ""), quote=True)


def pct(value):
    return "—" if value is None else "%d%%" % round(value * 100)


def render(graph, generated_at=None):
    nodes = graph["nodes"]
    report = graph.get("doctor") or {}
    by_id = {n["id"]: n for n in nodes}
    roots = [n for n in nodes if n["parent"] is None or n["parent"] not in by_id]

    banners = [n for n in roots
               if n["kind"] == model.KIND_GOAL
               and any(c["kind"] == model.KIND_GOAL
                       for c in model.children_of(nodes, n["id"]))]
    banner_ids = {n["id"] for n in banners}
    goal_cards = [n for n in nodes
                  if n["kind"] == model.KIND_GOAL and n["id"] not in banner_ids]

    parts = ["<!DOCTYPE html>", '<html lang="zh"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             "<title>roadmap radar</title>", "<style>%s</style>" % CSS,
             "</head><body>"]

    parts.append("<header>")
    for node in banners:
        parts.append('<div class="ns">%s <span class="pct">%s</span></div>'
                     % (esc(node["title"]), pct(node["progress"])))
    if not banners:
        parts.append('<div class="ns">roadmap radar</div>')
    parts.append('<div class="meta">%s · %d 个节点</div>'
                 % (esc(generated_at or ""), len(nodes)))
    parts.append("</header>")

    for goal in goal_cards:
        parts.append('<section class="goal-sec">%s</section>' % _tree(goal, nodes))

    parts.append(_unassigned(nodes, report, by_id))
    parts.append("</body></html>")
    return "\n".join(parts)


def _tree(root, nodes):
    return '<div class="tree">%s</div>' % _subtree(root, nodes)


def _subtree(node, nodes):
    out = [_node_box(node, nodes)]
    children = model.children_of(nodes, node["id"])
    if children:
        out.append("<ul>")
        for child in children:
            out.append("<li>%s</li>" % _subtree(child, nodes))
        out.append("</ul>")
    return "".join(out)


def _node_box(node, nodes):
    kind = node["kind"]
    classes = ["node", kind]
    done = kind == model.KIND_LEAF and (node["progress"] or 0) >= 1.0
    if done:
        classes.append("done-node")
    if "frontier" in node["badges"]:
        classes.append("frontier-node")
    if any(b in ("blocked", "orphan", "cycle", "unmapped-status")
           for b in node["badges"]):
        classes.append("warn-node")

    title = _link(node)
    if done:
        title = '<span class="done">%s</span>' % title

    head_extra = ""
    if kind == model.KIND_GOAL:
        head_extra = '<span class="pct">%s</span>' % pct(node["progress"])
    elif kind == model.KIND_GROUP:
        done_n, total_n = model.leaf_counts(nodes, node["id"])
        head_extra = ('<span class="pct"><b>%d/%d</b> · %s</span>'
                      % (done_n, total_n, pct(node["progress"])))

    out = ['<div class="%s">' % " ".join(classes),
           '<div class="t">%s%s</div>' % (head_extra, title)]

    value = node["progress"]
    if kind != model.KIND_LEAF and value is not None:
        out.append('<div class="bar%s"><i style="width:%d%%"></i></div>'
                   % (" full" if value >= 1.0 else "", round(value * 100)))

    meta = node["meta"] or {}
    summary = meta.get("summary") or meta.get("description")
    if summary:
        out.append('<div class="sum">%s</div>' % esc(summary))
    if kind == model.KIND_GOAL and meta.get("target_metric"):
        out.append('<div class="metric">🎯 %s</div>' % esc(meta["target_metric"]))

    tags = _node_tags(node)
    if tags:
        out.append('<div class="tags">%s</div>' % tags)
    out.append("</div>")
    return "".join(out)


def _node_tags(node):
    """右下角小标签：状态、badge、决策/迷雾计数（全文进 tooltip，不再罗列）。"""
    out = []
    for badge in node["badges"]:
        if badge == "frontier":
            out.append('<span class="tag frontier">frontier</span>')
        elif badge == model.BADGE_EMPTY:
            out.append('<span class="tag warn">空</span>')
        elif badge in ("blocked", "orphan", "unmapped-status", "cycle"):
            out.append('<span class="tag warn">%s</span>' % esc(badge))
        elif badge.startswith("claimed:"):
            out.append('<span class="tag">%s</span>' % esc(badge))
    if node["state"] and node["kind"] == model.KIND_LEAF:
        out.append('<span class="tag">%s</span>' % esc(node["state"]))
    meta = node["meta"] or {}
    for key, label in (("decisions", "决策"), ("fog", "迷雾")):
        items = meta.get(key) or []
        if items:
            out.append('<span class="tag" title="%s">%s %d</span>'
                       % (esc("\n".join(items)), label, len(items)))
    return "".join(out)


def _link(node):
    title = esc(node["title"])
    return '<a href="%s">%s</a>' % (esc(node["url"]), title) if node["url"] else title


def _unassigned(nodes, report, by_id):
    """未归位区。

    🔴 这里是「永不丢节点」的最后一道闸：任何挂不到 goal 下面的非 goal 节点都必须
    落在这里，**连它的整棵子树一起**。否则一张没打 goal label 的地图会带着它所有
    子票一起从页面上消失 —— 页面于是安静地少报了工作量。
    """
    unparented = [n for n in nodes
                  if n["kind"] != model.KIND_GOAL
                  and (n["parent"] is None or n["parent"] not in by_id)]
    dangling = report.get("dangling_parents") or []
    empty = report.get("empty_groups") or []
    unmapped = report.get("unmapped_status") or []
    if not (unparented or dangling or empty or unmapped):
        return ""
    out = ['<div id="unassigned"><h2>⚠️ 未归位 / 需要体检</h2>']
    if unparented:
        out.append('<div><b>没有挂到任何 goal（%d）</b><ul class="flat">'
                   % len(unparented))
        for node in unparented:
            reason = (node["meta"] or {}).get("reason") or ""
            tag = '<span class="tag">%s</span>' % esc(reason) if reason else ""
            out.append("<li>%s%s</li>" % (_tree(node, nodes), tag))
        out.append("</ul></div>")
    if dangling:
        out.append("<div><b>父节点找不到（迁仓断链）</b>"
                   '<table class="dangling">')
        for row in dangling:
            out.append("<tr><td>%s</td><td>→ 缺 %s</td></tr>"
                       % (esc(row.get("title")), esc(row.get("missing_parent"))))
        out.append("</table></div>")
    if empty:
        out.append("<div><b>空的 goal / 地图（charted 但没票）</b><ul>")
        for row in empty:
            out.append("<li>%s</li>" % esc(row.get("title")))
        out.append("</ul></div>")
    if unmapped:
        out.append("<div><b>状态词没映射</b><ul>")
        for row in unmapped:
            out.append("<li>%s · %s</li>"
                       % (esc(row.get("title")), esc(row.get("state"))))
        out.append("</ul></div>")
    out.append("</div>")
    return "".join(out)
