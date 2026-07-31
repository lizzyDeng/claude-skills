"""Node 图 -> 单文件 HTML。域无关：只认 Node 的字段，不认业务。

主视图 = issue 先后依赖 DAG（native blocking）：
每个 containment 根（map / 带子票的上游票）一条「泳道」，泳道内的布局
外包给 Graphviz（rankdir=LR：无前置的票在左，被 block 的票在其前置右边，
箭头 = 先做左边才能做右边）。dot 在**构建期**跑，页面仍是零 JS 的静态 SVG。

颜色语义（盒）：绿=已关，橘=正在做（有认领），灰=未开始；
frontier（前置全关 + 没人认领）加蓝色粗边框；
被 open blocker 卡住用红色箭头表达 —— 卡住是边的事实，不占盒的颜色。
泳道外的前置票画成虚线幽灵盒（跨泳道边不跨 SVG，就地给上下文）。
所有颜色都由页面 CSS class 控制（dot 不烘颜色），暗色模式自动适配。
"""

import html as html_mod
import subprocess

import model

FS = 12.5          # 标签像素宽估算的字号基准
CLIP_LABEL = 300   # 标签最大像素宽
DOT_BIN = "dot"

CSS = """
:root { color-scheme: light dark; --fg:#111; --dim:#666; --line:#ddd; --edge:#a8a8a8;
        --bar:#3b82f6; --ok:#16a34a; --doing:#f59e0b; --todo:#8b9199;
        --warn:#dc2626; --card:#fff; --bg:#fafafa; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e7e7e7; --dim:#999; --line:#333; --edge:#666; --todo:#7a8087;
          --card:#1a1a1a; --bg:#111; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
       "PingFang SC","Hiragino Sans GB",sans-serif; }
header { border-bottom:1px solid var(--line); padding-bottom:14px; margin-bottom:14px; }
header .ns { font-size:19px; font-weight:600; }
header .meta { color:var(--dim); font-size:12px; margin-top:4px; }
a { color:inherit; }

#legend { color:var(--dim); font-size:12px; margin-bottom:14px; }
#legend svg { vertical-align:-3px; margin:0 3px 0 12px; }

.lane { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:10px 16px 8px; margin-bottom:14px; overflow-x:auto; }
.lane-head { font-size:14.5px; }
.lane-head b { font-weight:600; }
.lane-head .cnt { color:var(--dim); font-size:12px; margin-left:8px;
                  font-variant-numeric:tabular-nums; }
.lane-head .sum { color:var(--dim); font-size:12px; margin-top:2px; }
.tag { display:inline-block; font-size:10.5px; padding:0 5px; border-radius:9px;
       border:1px solid var(--line); color:var(--dim); margin-left:6px; }

/* ---- graphviz SVG：几何来自 dot，颜色全在这里 ---- */
svg.dag { display:block; margin-top:4px; }
svg.dag text { fill:var(--fg); }
svg.dag .node path { fill:var(--card); stroke:var(--todo); stroke-width:1.3; }
svg.dag .node.todo path { fill:var(--todo); fill-opacity:.13; }
svg.dag .node.doing path { fill:var(--doing); fill-opacity:.18;
                                 stroke:var(--doing); }
svg.dag .node.done path { fill:var(--ok); fill-opacity:.15; stroke:var(--ok); }
svg.dag .node.done text { fill:var(--dim); text-decoration:line-through; }
svg.dag .node.frontier path { stroke:var(--bar); stroke-width:2.2; }
svg.dag .node.ghost path { fill:none; stroke:var(--dim); stroke-dasharray:3 3; }
svg.dag .node.ghost text { fill:var(--dim); }
svg.dag .edge path { fill:none; stroke:var(--edge); stroke-width:1.3; }
svg.dag .edge polygon { fill:var(--edge); stroke:var(--edge); }
svg.dag .edge.hot path { stroke:var(--warn); }
svg.dag .edge.hot polygon { fill:var(--warn); stroke:var(--warn); }

#goals { color:var(--dim); font-size:12px; margin-top:10px; }
#doctor { margin-top:26px; border:1px solid var(--warn); border-radius:10px;
          padding:12px 14px; overflow-x:auto; }
#doctor h2 { color:var(--warn); font-size:15px; margin:0 0 8px; }
table.dangling { border-collapse:collapse; font-size:12px; }
table.dangling td { padding:1px 10px 1px 0; }
"""

_SWATCH = ('<svg width="18" height="14"><rect x="1" y="1" width="16" height="12"'
           ' rx="3" style="%s"/></svg>')
LEGEND = (
    '<div id="legend">图例：'
    + _SWATCH % "fill:var(--ok);fill-opacity:.15;stroke:var(--ok)" + ' 已完成'
    + _SWATCH % "fill:var(--doing);fill-opacity:.18;stroke:var(--doing)"
    + ' 正在做（有认领）'
    + _SWATCH % "fill:var(--todo);fill-opacity:.13;stroke:var(--todo)" + ' 未开始'
    + _SWATCH % ("fill:var(--todo);fill-opacity:.13;stroke:var(--bar);"
                 "stroke-width:2.2") + ' frontier（前置全关，可上手）'
    + '<svg width="30" height="14"><path d="M2 7 H22" style="stroke:var(--warn);'
      'stroke-width:1.6"/><path d="M22 3 L28 7 L22 11 Z" '
      'style="fill:var(--warn)"/></svg> 被未完成的前置卡住'
    + _SWATCH % "fill:none;stroke:var(--dim);stroke-dasharray:3 3" + ' 泳道外的前置票'
    + '</div>')


def esc(value):
    return html_mod.escape(str(value if value is not None else ""), quote=True)


def _dq(value):
    """DOT 双引号字符串转义。"""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _text_w(text, size=FS):
    """CJK 记全宽、其他记 0.6 宽的像素宽度估算（只用于截断，不做布局）。"""
    width = 0.0
    for ch in text:
        width += size if ord(ch) >= 0x2E80 else size * 0.6
    return width


def _clip(text, max_w=CLIP_LABEL):
    if _text_w(text) <= max_w:
        return text
    out = ""
    for ch in text:
        if _text_w(out + ch) > max_w - FS:
            return out + "…"
        out += ch
    return out


def _is_done(node):
    return (node["progress"] or 0) >= 1.0 \
        or str(node["state"] or "").lower() == "closed"


def render(graph, generated_at=None):
    nodes = graph["nodes"]
    report = graph.get("doctor") or {}
    by_id = {n["id"]: n for n in nodes}
    kids = model.children_map(nodes)

    def is_top(n):
        parent = n["parent"]
        return (parent is None or parent not in by_id
                or by_id[parent]["kind"] == model.KIND_GOAL)

    non_goal = [n for n in nodes if n["kind"] != model.KIND_GOAL]
    lanes = [n for n in non_goal if is_top(n) and kids.get(n["id"])]
    strays = [n for n in non_goal if is_top(n) and not kids.get(n["id"])]
    goals = [n for n in nodes if n["kind"] == model.KIND_GOAL]

    parts = ["<!DOCTYPE html>", '<html lang="zh"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             "<title>roadmap radar</title>", "<style>%s</style>" % CSS,
             "</head><body>"]
    parts.append("<header>")
    parts.append('<div class="ns">issue 依赖 radar</div>')
    parts.append('<div class="meta">%s · %d 个节点 · 箭头 = 先做左边才能做右边</div>'
                 % (esc(generated_at or ""), len(nodes)))
    parts.append("</header>")
    parts.append(LEGEND)

    for lane in lanes:
        parts.append(_lane_section(lane, by_id, kids))
    if strays:
        parts.append(_stray_section(strays, by_id))
    if goals:
        parts.append('<div id="goals">goal 标签（不参与布局）：%s</div>'
                     % " · ".join(esc(g["title"]) for g in goals))

    parts.append(_doctor(report))
    parts.append("</body></html>")
    return "\n".join(parts)


def _descendants(root, kids):
    out = []
    stack = list(kids.get(root["id"], []))
    seen = {root["id"]}
    while stack:
        node = stack.pop()
        if node["id"] in seen:
            continue
        seen.add(node["id"])
        out.append(node)
        stack.extend(kids.get(node["id"], []))
    return out


def _lane_section(root, by_id, kids):
    members = _descendants(root, kids)
    done = sum(1 for m in members if _is_done(m))
    chips = ""
    parent = by_id.get(root["parent"] or "")
    if parent is not None and parent["kind"] == model.KIND_GOAL:
        chips = '<span class="tag">%s</span>' % esc(parent["title"])
    out = ['<section class="lane">',
           '<div class="lane-head" title="%s"><b>%s</b>'
           '<span class="cnt">%d/%d</span>%s</div>'
           % (esc(_tooltip(root)), _link(root), done, len(members), chips)]
    summary = (root["meta"] or {}).get("summary")
    if summary:
        out.append('<div class="lane-head"><div class="sum">%s</div></div>'
                   % esc(summary))
    out.append(_dag_svg(members, by_id))
    out.append("</section>")
    return "".join(out)


def _stray_section(strays, by_id):
    return ('<section class="lane">'
            '<div class="lane-head"><b>散票（不在任何地图 / 上游票下）</b>'
            '<span class="cnt">%d</span></div>%s</section>'
            % (len(strays), _dag_svg(strays, by_id)))


# ---------------- DOT 生成 + graphviz 渲染 ----------------

def _dag_svg(members, by_id):
    return _dot_to_svg(_to_dot(members, by_id))


def _to_dot(members, by_id):
    """成员 + 泳道外幽灵前置票 → DOT。布局交给 graphviz，颜色只打 class。"""
    ids = {m["id"] for m in members}
    ghosts = {}
    for m in members:
        for b in (m["meta"] or {}).get("blocked_by") or []:
            if b not in ids and b not in ghosts:
                ghosts[b] = by_id.get(b) or model.node(b, model.KIND_LEAF, b)

    out = ["digraph radar {",
           '  rankdir=LR; bgcolor="transparent";',
           '  pack=true; packmode="array_c1";',
           '  graph [nodesep=0.1, ranksep=0.6, margin=0.05];',
           '  node [shape=box, style=rounded, fontsize=11.5, height=0.32,'
           ' margin="0.14,0.07", fontname="Helvetica,PingFang SC"];',
           '  edge [arrowsize=0.7];']

    for m in members:
        out.append('  "%s" [label="%s", class="%s", href="%s",'
                   ' tooltip="%s"];'
                   % (_dq(m["id"]), _dq(_clip(m["title"])), _node_class(m),
                      _dq(m["url"] or "#"), _dq(_tooltip(m))))
    for gid, ghost in ghosts.items():
        out.append('  "%s" [label="%s", class="ghost", href="%s",'
                   ' tooltip="%s"];'
                   % (_dq(gid), _dq(_ghost_label(ghost)),
                      _dq(ghost["url"] or "#"), _dq(_tooltip(ghost))))

    known = ids | set(ghosts)
    for m in members:
        for b in (m["meta"] or {}).get("blocked_by") or []:
            if b not in known:
                continue
            blocker = by_id.get(b) or ghosts.get(b)
            hot = blocker is not None and not _is_done(blocker)
            out.append('  "%s" -> "%s" [class="dep%s"];'
                       % (_dq(b), _dq(m["id"]), " hot" if hot else ""))
    out.append("}")
    return "\n".join(out)


def _node_class(node):
    if _is_done(node):
        cls = "done"
    elif any(b.startswith("claimed:") for b in node["badges"]):
        cls = "doing"
    else:
        cls = "todo"
    if "frontier" in node["badges"]:
        cls += " frontier"
    return cls


def _ghost_label(node):
    title = node["title"]
    number = (node["meta"] or {}).get("number")
    if number:
        title = "#%s %s" % (number, title)
    return _clip(title)


def _dot_to_svg(dot_text):
    """dot -Tsvg，剥掉 XML 头只留 <svg>，挂上 class="dag"。"""
    try:
        proc = subprocess.run([DOT_BIN, "-Tsvg"], input=dot_text,
                              capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit("需要 graphviz 做 DAG 布局：brew install graphviz")
    if proc.returncode != 0:
        raise RuntimeError("dot 渲染失败：%s" % proc.stderr.strip()[:400])
    svg = proc.stdout[proc.stdout.index("<svg"):]
    return svg.replace("<svg ", '<svg class="dag" ', 1)


def _link(node):
    title = esc(node["title"])
    return '<a href="%s">%s</a>' % (esc(node["url"]), title) if node["url"] else title


def _tooltip(node):
    """hover 全文：完整标题 + 一句话简介 + 状态/认领/badge + 决策/迷雾全文。"""
    meta = node["meta"] or {}
    lines = [node["title"]]
    summary = meta.get("summary") or meta.get("description")
    if summary:
        lines.append(summary)
    status_bits = []
    if node["state"]:
        status_bits.append(node["state"])
    status_bits += node["badges"]
    if status_bits:
        lines.append(" · ".join(status_bits))
    for key, label in (("decisions", "决策"), ("fog", "迷雾")):
        items = meta.get(key) or []
        if items:
            lines.append("%s %d:" % (label, len(items)))
            lines.extend("- %s" % item for item in items)
    return "\n".join(lines)


def _doctor(report):
    """体检区：断链 / 空地图 / 状态词没映射。只报告不修。"""
    dangling = report.get("dangling_parents") or []
    empty = report.get("empty_groups") or []
    unmapped = report.get("unmapped_status") or []
    if not (dangling or empty or unmapped):
        return ""
    out = ['<div id="doctor"><h2>⚠️ 体检</h2>']
    if dangling:
        out.append('<div><b>父节点找不到（迁仓断链）</b><table class="dangling">')
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
