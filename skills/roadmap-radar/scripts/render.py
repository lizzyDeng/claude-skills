"""Node 树 -> 单文件 HTML。域无关：只认 Node 的字段，不认业务。

每个 goal 渲成一张「圆点 + 连线」的 tidy tree（d3 树图的经典画法）：
goal 在左、子树逐层向右，Python 算好布局直接吐 SVG —— 零 JS、无 CDN、
双击即开的约束不变。点的颜色编码状态（实绿=完成、实蓝=frontier、
实红=blocked/断链、空心=未动/进行中），标题/一句话简介/决策全文进 hover
tooltip（SVG <title>）。
"""

import html as html_mod

import model

FS = 12.5          # 标签字号（px），布局的宽度估算基于它
ROW = 24           # 每个叶子占的行高
PAD = 16           # SVG 四周留白
R = 4.5            # 普通节点圆点半径
R_GOAL = 6.5       # goal 根节点半径
LABEL_PAD = 8      # 圆点到标签的间距
GAP_MIN = 60       # 相邻两层圆点列的最小间距
CLIP_ROOT, CLIP_MID, CLIP_LEAF = 300, 230, 340   # 各类标签的最大像素宽

CSS = """
:root { color-scheme: light dark; --fg:#111; --dim:#666; --line:#ddd; --edge:#bbb;
        --bar:#3b82f6; --ok:#16a34a; --warn:#dc2626; --card:#fff; --bg:#fafafa; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e7e7e7; --dim:#999; --line:#333; --edge:#555;
          --card:#1a1a1a; --bg:#111; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
       "PingFang SC","Hiragino Sans GB",sans-serif; }
header { border-bottom:1px solid var(--line); padding-bottom:14px; margin-bottom:14px; }
header .ns { font-size:19px; font-weight:600; }
header .meta { color:var(--dim); font-size:12px; margin-top:4px; }
.pct { font-variant-numeric:tabular-nums; font-weight:600; float:right;
       margin-left:8px; font-size:12px; }
.bar { height:4px; background:var(--line); border-radius:2px; overflow:hidden;
       margin:6px 0 4px; max-width:340px; }
.bar > i { display:block; height:100%; background:var(--bar); }
.bar.full > i { background:var(--ok); }
a { color:inherit; }

#legend { color:var(--dim); font-size:12px; margin-bottom:14px; }
#legend svg { vertical-align:-2px; margin:0 3px 0 10px; }

.goal-sec { background:var(--card); border:1px solid var(--line); border-radius:10px;
            padding:12px 16px 6px; margin-bottom:14px; overflow-x:auto; }
.goal-head b { font-size:15px; }
.goal-head .sum { color:var(--dim); font-size:12px; margin-top:2px; }
.goal-head .metric { color:var(--dim); font-size:11.5px; margin-top:2px;
                     overflow-wrap:anywhere; }
.tag { display:inline-block; font-size:10.5px; padding:0 5px; border-radius:9px;
       border:1px solid var(--line); color:var(--dim); margin-left:5px; }

svg.tree { display:block; }
svg.tree text { font-size:12.5px; fill:var(--fg);
                paint-order:stroke; stroke:var(--card); stroke-width:3px;
                stroke-linejoin:round; }
svg.tree text.strike { fill:var(--dim); text-decoration:line-through; }
svg.tree tspan.dim { fill:var(--dim); font-size:11px; }
svg.tree tspan.warntx { fill:var(--warn); font-size:11px; }
svg.tree path.edge { fill:none; stroke:var(--edge); stroke-width:1.3; }
svg.tree circle.dot { fill:var(--card); stroke:var(--dim); stroke-width:1.6; }
svg.tree circle.goaldot { stroke:var(--fg); stroke-width:2; }
svg.tree circle.part { stroke:var(--bar); }
svg.tree circle.done { fill:var(--ok); stroke:var(--ok); }
svg.tree circle.frontier { fill:var(--bar); stroke:var(--bar); }
svg.tree circle.warn { fill:var(--warn); stroke:var(--warn); }

#unassigned { margin-top:26px; border:1px solid var(--warn); border-radius:10px;
              padding:12px 14px; overflow-x:auto; }
#unassigned h2 { color:var(--warn); font-size:15px; margin:0 0 8px; }
#unassigned ul.flat { list-style:none; margin:5px 0; padding:0; }
#unassigned ul.flat > li { margin:2px 0; }
table.dangling { border-collapse:collapse; font-size:12px; }
table.dangling td { padding:1px 10px 1px 0; }
"""

LEGEND = (
    '<div id="legend">图例：'
    '%s 完成 %s frontier（现在能动） %s blocked / 断链 %s 进行中 %s 未动'
    '</div>'
) % tuple(
    '<svg width="12" height="12"><circle cx="6" cy="6" r="4.5" class="%s" '
    'style="fill:%s;stroke:%s;stroke-width:1.6"/></svg>' % (cls, fill, stroke)
    for cls, fill, stroke in (
        ("", "var(--ok)", "var(--ok)"),
        ("", "var(--bar)", "var(--bar)"),
        ("", "var(--warn)", "var(--warn)"),
        ("", "var(--card)", "var(--bar)"),
        ("", "var(--card)", "var(--dim)"),
    ))


def esc(value):
    return html_mod.escape(str(value if value is not None else ""), quote=True)


def pct(value):
    return "—" if value is None else "%d%%" % round(value * 100)


def _text_w(text, size=FS):
    """无字体度量环境下的宽度估算：CJK 记全宽，其他记 0.6 宽。"""
    width = 0.0
    for ch in text:
        width += size if ord(ch) >= 0x2E80 else size * 0.6
    return width


def _clip(text, max_w):
    if _text_w(text) <= max_w:
        return text
    out = ""
    for ch in text:
        if _text_w(out + ch) > max_w - FS:
            return out + "…"
        out += ch
    return out


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
    parts.append(LEGEND)

    for goal in goal_cards:
        parts.append(_goal_section(goal, nodes))

    parts.append(_unassigned(nodes, report, by_id))
    parts.append("</body></html>")
    return "\n".join(parts)


def _goal_section(goal, nodes):
    value = goal["progress"]
    meta = goal["meta"] or {}
    out = ['<section class="goal-sec">', '<div class="goal-head">',
           '<b>%s</b> <span class="pct">%s</span>' % (esc(goal["title"]), pct(value))]
    if value is not None:
        out.append('<div class="bar%s"><i style="width:%d%%"></i></div>'
                   % (" full" if value >= 1.0 else "", round(value * 100)))
    if model.BADGE_EMPTY in goal["badges"]:
        out.append('<span class="tag">空</span>')
    summary = meta.get("summary") or meta.get("description")
    if summary:
        out.append('<div class="sum">%s</div>' % esc(summary))
    if meta.get("target_metric"):
        out.append('<div class="metric">🎯 %s</div>' % esc(meta["target_metric"]))
    out.append("</div>")
    out.append(_tree_svg(goal, nodes))
    out.append("</section>")
    return "".join(out)


# ---------------- tidy tree 布局 + SVG ----------------

def _tree_svg(root, nodes):
    """root 的整棵子树 -> 一张 <svg>：圆点 + 贝塞尔连线 + 带描边底的标签。"""
    kids = {}
    order = []

    def gather(n, depth, seen):
        if n["id"] in seen:      # 环：rollup 已打 badge，布局直接不往下走
            return
        seen.add(n["id"])
        children = model.children_of(nodes, n["id"])
        kids[n["id"]] = children
        order.append((n, depth))
        for c in children:
            gather(c, depth + 1, seen)

    gather(root, 0, set())

    # y：叶子按 DFS 依次占行，内部节点取子节点均值
    ys = {}
    next_row = [0]

    def place(n):
        children = kids[n["id"]]
        if not children:
            ys[n["id"]] = next_row[0]
            next_row[0] += 1
        else:
            for c in children:
                place(c)
            child_ys = [ys[c["id"]] for c in children]
            ys[n["id"]] = sum(child_ys) / len(child_ys)

    place(root)

    # x：每层一列。层间距要装得下该层「内部节点」的左置标签
    depth_of = {n["id"]: d for n, d in order}
    max_depth = max(depth_of.values())
    labels = {n["id"]: _label_parts(n, kids[n["id"]], n is root) for n, _ in order}

    def label_w(n):
        main, note = labels[n["id"]]
        return _text_w(main) + _text_w(note, 11)

    col_x = {0: PAD + label_w(root) + LABEL_PAD + R_GOAL}
    for depth in range(1, max_depth + 1):
        inner_w = [label_w(n) for n, d in order if d == depth and kids[n["id"]]]
        gap = max(GAP_MIN, (max(inner_w) if inner_w else 0) + LABEL_PAD + 18)
        col_x[depth] = col_x[depth - 1] + gap

    width = PAD + max(
        col_x[d] + R + ((LABEL_PAD + label_w(n)) if not kids[n["id"]] else 0)
        for n, d in order)
    height = max(1, next_row[0]) * ROW + PAD * 2

    def xy(n):
        return col_x[depth_of[n["id"]]], PAD + ROW / 2 + ys[n["id"]] * ROW

    parts = ['<svg class="tree" width="%d" height="%d" '
             'xmlns="http://www.w3.org/2000/svg">' % (round(width), round(height))]
    for n, _ in order:                    # 先画边，再画点，点压边上
        x0, y0 = xy(n)
        for c in kids[n["id"]]:
            x1, y1 = xy(c)
            mx = (x0 + x1) / 2
            parts.append('<path class="edge" d="M%.1f %.1f C%.1f %.1f %.1f %.1f '
                         '%.1f %.1f"/>' % (x0 + R, y0, mx, y0, mx, y1, x1 - R, y1))
    for n, depth in order:
        parts.append(_node_svg(n, kids[n["id"]], depth == 0,
                               labels[n["id"]], xy(n)))
    parts.append("</svg>")
    return "".join(parts)


def _label_parts(node, children, is_root):
    """(主标签, 附注)。附注 = 空地图的「空」，或内部节点的 done/total·pct。"""
    title = _clip(node["title"],
                  CLIP_ROOT if is_root else (CLIP_MID if children else CLIP_LEAF))
    if model.BADGE_EMPTY in node["badges"] and node["kind"] != model.KIND_LEAF:
        return title, " 空"
    if children:
        done_n = sum(1 for c in children if (c["progress"] or 0) >= 1.0)
        note = " %d/%d" % (done_n, len(children))
        if node["progress"] is not None:
            note += " · %s" % pct(node["progress"])
        return title, note
    return title, ""


def _node_svg(node, children, is_root, label_parts, pos):
    x, y = pos
    badges = node["badges"]
    done = (node["progress"] or 0) >= 1.0 and node["kind"] == model.KIND_LEAF \
        and not children
    warn = any(b in ("blocked", "orphan", "unmapped-status", "cycle")
               for b in badges)

    cls = "dot"
    if is_root and node["kind"] == model.KIND_GOAL:
        cls += " goaldot"
    if warn:
        cls += " warn"
    elif "frontier" in badges:
        cls += " frontier"
    elif done or str(node["state"] or "").lower() == "closed":
        cls += " done"
    elif node["progress"] is not None and 0 < node["progress"] < 1:
        cls += " part"

    radius = R_GOAL if is_root else R
    label, note = label_parts
    anchor_left = bool(children) or is_root
    tx = x - radius - LABEL_PAD if anchor_left else x + radius + LABEL_PAD
    anchor = "end" if anchor_left else "start"

    extra = ""
    if note == " 空":
        extra = '<tspan class="warntx"> 空</tspan>'
    elif note:
        extra = '<tspan class="dim">%s</tspan>' % esc(note)

    text_cls = ' class="strike"' if done else ""
    body = ('<circle class="%s" cx="%.1f" cy="%.1f" r="%.1f"/>'
            '<text x="%.1f" y="%.1f" text-anchor="%s"%s>%s%s</text>'
            '<title>%s</title>'
            % (cls, x, y, radius, tx, y + 4, anchor, text_cls, esc(label),
               extra, esc(_tooltip(node))))
    if node["url"]:
        return '<a class="n" data-id="%s" href="%s">%s</a>' \
            % (esc(node["id"]), esc(node["url"]), body)
    return '<g class="n" data-id="%s">%s</g>' % (esc(node["id"]), body)


def _tooltip(node):
    """hover 全文：完整标题 + 一句话简介 + 状态/认领 + 决策/迷雾全文。"""
    meta = node["meta"] or {}
    lines = [node["title"]]
    summary = meta.get("summary") or meta.get("description")
    if summary:
        lines.append(summary)
    status_bits = [b for b in node["badges"] if not b.startswith("claimed:")]
    status_bits += [b for b in node["badges"] if b.startswith("claimed:")]
    if node["state"]:
        status_bits.insert(0, node["state"])
    if status_bits:
        lines.append(" · ".join(status_bits))
    for key, label in (("decisions", "决策"), ("fog", "迷雾")):
        items = meta.get(key) or []
        if items:
            lines.append("%s %d:" % (label, len(items)))
            lines.extend("- %s" % item for item in items)
    return "\n".join(lines)


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
            out.append("<li>%s%s</li>" % (_tree_svg(node, nodes), tag))
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
