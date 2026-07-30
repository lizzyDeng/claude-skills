"""Node 树 -> 单文件 HTML。域无关：只认 Node 的字段，不认业务。

展开靠原生 <details>，所以零 JS。CSS 内联，无 CDN，双击即开。
"""

import html as html_mod

import model

CSS = """
:root { color-scheme: light dark; --fg:#111; --dim:#666; --line:#ddd;
        --bar:#3b82f6; --ok:#16a34a; --warn:#dc2626; --card:#fff; --bg:#fafafa; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e7e7e7; --dim:#999; --line:#333; --card:#1a1a1a; --bg:#111; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
       font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",
       "PingFang SC","Hiragino Sans GB",sans-serif; }
header { border-bottom:1px solid var(--line); padding-bottom:14px; margin-bottom:20px; }
header .ns { font-size:19px; font-weight:600; }
header .meta { color:var(--dim); font-size:12px; margin-top:4px; }
.cards { display:grid; gap:12px; grid-template-columns:repeat(auto-fill,minmax(268px,1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:12px 14px; }
.card h2 { font-size:15px; margin:0 0 2px; }
.card .metric { color:var(--dim); font-size:11.5px; margin-bottom:9px;
                overflow-wrap:anywhere; }
.pct { font-variant-numeric:tabular-nums; font-weight:600; float:right; }
.bar { height:6px; background:var(--line); border-radius:3px; overflow:hidden;
       margin-bottom:10px; }
.bar > i { display:block; height:100%; background:var(--bar); }
.bar.full > i { background:var(--ok); }
details { margin:5px 0; }
summary { cursor:pointer; }
ul { list-style:none; margin:5px 0 5px 14px; padding:0;
     border-left:1px solid var(--line); }
li { padding:2px 0 2px 10px; }
a { color:inherit; }
.tag { display:inline-block; font-size:10.5px; padding:0 5px; border-radius:9px;
       border:1px solid var(--line); color:var(--dim); margin-left:5px;
       vertical-align:1px; }
.tag.frontier { border-color:var(--bar); color:var(--bar); }
.tag.warn { border-color:var(--warn); color:var(--warn); }
.done { color:var(--dim); text-decoration:line-through; }
.sections { color:var(--dim); font-size:12px; margin:3px 0 3px 14px; }
#unassigned { margin-top:26px; border:1px solid var(--warn); border-radius:10px;
              padding:12px 14px; }
#unassigned h2 { color:var(--warn); font-size:15px; margin:0 0 8px; }
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

    parts.append('<div class="cards">')
    for goal in goal_cards:
        parts.append(_goal_card(goal, nodes))
    parts.append("</div>")

    parts.append(_unassigned(nodes, report, by_id))
    parts.append("</body></html>")
    return "\n".join(parts)


def _goal_card(goal, nodes):
    value = goal["progress"]
    out = ['<div class="card">',
           '<h2>%s <span class="pct">%s</span></h2>' % (esc(goal["title"]), pct(value)),
           '<div class="bar%s"><i style="width:%d%%"></i></div>'
           % (" full" if (value or 0) >= 1.0 else "", round((value or 0) * 100))]
    metric = (goal["meta"] or {}).get("target_metric")
    if metric:
        out.append('<div class="metric">%s</div>' % esc(metric))
    for child in model.children_of(nodes, goal["id"]):
        out.append(_group_or_leaf(child, nodes))
    out.append("</div>")
    return "".join(out)


def _group_or_leaf(node, nodes):
    if node["kind"] == model.KIND_LEAF:
        return "<div>%s</div>" % _leaf_line(node)
    done, total = model.leaf_counts(nodes, node["id"])
    tags = ""
    if model.BADGE_EMPTY in node["badges"]:
        tags = '<span class="tag warn">空地图</span>'
    out = ["<details open><summary>%s <b>%d/%d</b>%s %s</summary>"
           % (_link(node), done, total, tags, pct(node["progress"]))]
    meta = node["meta"] or {}
    for key, label in (("decisions", "决策"), ("fog", "迷雾")):
        for item in meta.get(key) or []:
            out.append('<div class="sections">%s · %s</div>' % (label, esc(item)))
    children = model.children_of(nodes, node["id"])
    if children:
        out.append("<ul>")
        for child in children:
            out.append("<li>%s</li>" % _leaf_line(child))
        out.append("</ul>")
    out.append("</details>")
    return "".join(out)


def _leaf_line(node):
    done = (node["progress"] or 0) >= 1.0
    body = '<span class="%s">%s</span>' % ("done" if done else "", _link(node))
    for badge in node["badges"]:
        if badge == "frontier":
            body += '<span class="tag frontier">frontier</span>'
        elif badge in ("blocked", "orphan", "unmapped-status", "cycle"):
            body += '<span class="tag warn">%s</span>' % esc(badge)
        elif badge.startswith("claimed:"):
            body += '<span class="tag">%s</span>' % esc(badge)
    if node["state"] and node["kind"] == model.KIND_LEAF:
        body += '<span class="tag">%s</span>' % esc(node["state"])
    return body


def _link(node):
    title = esc(node["title"])
    return '<a href="%s">%s</a>' % (esc(node["url"]), title) if node["url"] else title


def _unassigned(nodes, report, by_id):
    orphans = [n for n in nodes if "orphan" in n["badges"]]
    dangling = report.get("dangling_parents") or []
    empty = report.get("empty_groups") or []
    unmapped = report.get("unmapped_status") or []
    if not (orphans or dangling or empty or unmapped):
        return ""
    out = ['<div id="unassigned"><h2>⚠️ 未归位 / 需要体检</h2>']
    if orphans:
        out.append("<div><b>没有归属的节点（%d）</b><ul>" % len(orphans))
        for node in orphans:
            reason = (node["meta"] or {}).get("reason") or ""
            out.append("<li>%s <span class=\"tag\">%s</span></li>"
                       % (_link(node), esc(reason)))
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
