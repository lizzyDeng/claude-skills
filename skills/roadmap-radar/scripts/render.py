"""Node 图 -> 单文件 HTML。域无关：只认 Node 的字段，不认业务。

主视图 = issue 先后依赖 DAG（native blocking）：
每个 containment 根（map / 带子票的上游票）一条「泳道」，泳道内按
longest-path 分层 —— 无前置的票在第 0 列，被 block 的票 = max(前置列)+1，
箭头从左指右（先做的在左）。圆点 + 贝塞尔连线，Python 直出 SVG，零 JS。

颜色语义（点）：绿=已关，橘=正在做（有认领），灰=未开始；
frontier（前置全关 + 没人认领）在灰点外加蓝圈；
被 open blocker 卡住用红色箭头表达 —— 卡住是边的事实，不占点的颜色。
泳道外的前置票画成虚线幽灵点（跨泳道边不跨 SVG，就地给上下文）。
"""

import html as html_mod

import model

FS = 12.5          # 标签字号（px），布局宽度估算基于它
ROW = 26           # 行高
PAD = 16           # SVG 四周留白
R = 5              # 圆点半径
LABEL_PAD = 8      # 圆点到标签的间距
GAP_MIN = 80       # 相邻两列的最小间距
CLIP_LABEL = 300   # 标签最大像素宽

CSS = """
:root { color-scheme: light dark; --fg:#111; --dim:#666; --line:#ddd; --edge:#b5b5b5;
        --bar:#3b82f6; --ok:#16a34a; --doing:#f59e0b; --todo:#a3a8af;
        --warn:#dc2626; --card:#fff; --bg:#fafafa; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e7e7e7; --dim:#999; --line:#333; --edge:#555; --todo:#6c727a;
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
#legend svg { vertical-align:-2px; margin:0 3px 0 12px; }

.lane { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:10px 16px 4px; margin-bottom:14px; overflow-x:auto; }
.lane-head { font-size:14.5px; }
.lane-head b { font-weight:600; }
.lane-head .cnt { color:var(--dim); font-size:12px; margin-left:8px;
                  font-variant-numeric:tabular-nums; }
.lane-head .sum { color:var(--dim); font-size:12px; margin-top:2px; }
.tag { display:inline-block; font-size:10.5px; padding:0 5px; border-radius:9px;
       border:1px solid var(--line); color:var(--dim); margin-left:6px; }

svg.dag { display:block; }
svg.dag text { font-size:12.5px; fill:var(--fg);
               paint-order:stroke; stroke:var(--card); stroke-width:3px;
               stroke-linejoin:round; }
svg.dag text.strike { fill:var(--dim); text-decoration:line-through; }
svg.dag text.ghosttx { fill:var(--dim); font-size:11.5px; }
svg.dag path.dep { fill:none; stroke:var(--edge); stroke-width:1.4; }
svg.dag path.dep.hot { stroke:var(--warn); }
svg.dag .arwfill { fill:var(--edge); }
svg.dag .arwfill-hot { fill:var(--warn); }
svg.dag circle.done { fill:var(--ok); stroke:var(--ok); stroke-width:1.6; }
svg.dag circle.doing { fill:var(--doing); stroke:var(--doing); stroke-width:1.6; }
svg.dag circle.todo { fill:var(--todo); stroke:var(--todo); stroke-width:1.6; }
svg.dag circle.frontier { stroke:var(--bar); stroke-width:2.4; }
svg.dag circle.ghost { fill:var(--card); stroke:var(--dim); stroke-width:1.4;
                       stroke-dasharray:2.5 2.5; }

#goals { color:var(--dim); font-size:12px; margin-top:10px; }
#doctor { margin-top:26px; border:1px solid var(--warn); border-radius:10px;
          padding:12px 14px; overflow-x:auto; }
#doctor h2 { color:var(--warn); font-size:15px; margin:0 0 8px; }
table.dangling { border-collapse:collapse; font-size:12px; }
table.dangling td { padding:1px 10px 1px 0; }
"""

_LEGEND_DOT = ('<svg width="14" height="14"><circle cx="7" cy="7" r="5" '
               'style="%s"/></svg>')
LEGEND = (
    '<div id="legend">图例：'
    + _LEGEND_DOT % "fill:var(--ok);stroke:var(--ok)" + ' 已完成'
    + _LEGEND_DOT % "fill:var(--doing);stroke:var(--doing)" + ' 正在做（有认领）'
    + _LEGEND_DOT % "fill:var(--todo);stroke:var(--todo)" + ' 未开始'
    + _LEGEND_DOT % ("fill:var(--todo);stroke:var(--bar);stroke-width:2.4")
    + ' frontier（前置全关，可上手）'
    + '<svg width="30" height="14"><path d="M2 7 H22" style="stroke:var(--warn);'
      'stroke-width:1.6"/><path d="M22 3 L28 7 L22 11 Z" '
      'style="fill:var(--warn)"/></svg> 被未完成的前置卡住'
    + _LEGEND_DOT % ("fill:var(--card);stroke:var(--dim);stroke-dasharray:2.5 2.5")
    + ' 泳道外的前置票'
    + '</div>')


def esc(value):
    return html_mod.escape(str(value if value is not None else ""), quote=True)


def _text_w(text, size=FS):
    """无字体度量环境下的宽度估算：CJK 记全宽，其他记 0.6 宽。"""
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

    for index, lane in enumerate(lanes):
        parts.append(_lane_section(lane, nodes, by_id, kids, "l%d" % index))
    if strays:
        parts.append(_stray_section(strays, by_id, "stray"))
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


def _lane_section(root, nodes, by_id, kids, uid):
    members = _descendants(root, kids)
    done = sum(1 for m in members if _is_done(m))
    head = _link(root)
    chips = ""
    parent = by_id.get(root["parent"] or "")
    if parent is not None and parent["kind"] == model.KIND_GOAL:
        chips = '<span class="tag">%s</span>' % esc(parent["title"])
    out = ['<section class="lane">',
           '<div class="lane-head" title="%s"><b>%s</b>'
           '<span class="cnt">%d/%d</span>%s</div>'
           % (esc(_tooltip(root)), head, done, len(members), chips)]
    summary = (root["meta"] or {}).get("summary")
    if summary:
        out.append('<div class="lane-head"><div class="sum">%s</div></div>'
                   % esc(summary))
    out.append(_dag_svg(members, by_id, uid))
    out.append("</section>")
    return "".join(out)


def _stray_section(strays, by_id, uid):
    return ('<section class="lane">'
            '<div class="lane-head"><b>散票（不在任何地图 / 上游票下）</b>'
            '<span class="cnt">%d</span></div>%s</section>'
            % (len(strays), _dag_svg(strays, by_id, uid)))


# ---------------- DAG 布局 + SVG ----------------

def _dag_svg(members, by_id, uid):
    """成员 + 泳道外幽灵前置票 → 分层 DAG SVG。"""
    ids = {m["id"] for m in members}
    ghosts = {}
    for m in members:
        for b in (m["meta"] or {}).get("blocked_by") or []:
            if b not in ids and b not in ghosts:
                ghosts[b] = by_id.get(b) or model.node(b, model.KIND_LEAF, b)
    pop = {m["id"]: m for m in members}
    pop.update(ghosts)

    def blockers_of(nid):
        node = pop.get(nid)
        if node is None or nid in ghosts:      # 幽灵只当源头，不再往上追
            return []
        return [b for b in (node["meta"] or {}).get("blocked_by") or []
                if b in pop]

    layer = {}

    def resolve(nid, trail):
        if nid in layer:
            return layer[nid]
        if nid in trail:                        # 依赖成环：断开，当源头
            return 0
        blockers = blockers_of(nid)
        value = 0 if not blockers else 1 + max(
            resolve(b, trail | {nid}) for b in blockers)
        layer[nid] = value
        return value

    for nid in pop:
        resolve(nid, set())

    # 行：逐列从左到右，尽量贴前置的行（链条走直线），冲突就找空行
    max_layer = max(layer.values()) if layer else 0
    rows = {}
    used = {}
    for col in range(0, max_layer + 1):
        col_ids = [nid for nid in pop if layer[nid] == col]
        col_ids.sort(key=lambda nid: (
            sum(rows[b] for b in blockers_of(nid) if b in rows)
            / max(1, len([b for b in blockers_of(nid) if b in rows]))
            if any(b in rows for b in blockers_of(nid)) else 1e9))
        taken = used.setdefault(col, set())
        free = 0
        for nid in col_ids:
            ref = [rows[b] for b in blockers_of(nid) if b in rows]
            want = round(sum(ref) / len(ref)) if ref else None
            if want is None or want in taken:
                while free in taken:
                    free += 1
                want = free
            taken.add(want)
            rows[nid] = want

    labels = {nid: _label_of(pop[nid], nid in ghosts) for nid in pop}
    col_x = {0: PAD + R}
    for col in range(1, max_layer + 1):
        widths = [_text_w(labels[nid]) for nid in pop if layer[nid] == col - 1]
        col_x[col] = col_x[col - 1] + max(GAP_MIN,
                                          (max(widths) if widths else 0)
                                          + LABEL_PAD + 34)
    width = PAD + max(col_x[layer[nid]] + R + LABEL_PAD + _text_w(labels[nid])
                      for nid in pop)
    height = (max(rows.values()) + 1) * ROW + PAD * 2 if rows else ROW

    def xy(nid):
        return col_x[layer[nid]], PAD + ROW / 2 + rows[nid] * ROW

    parts = ['<svg class="dag" width="%d" height="%d" '
             'xmlns="http://www.w3.org/2000/svg">' % (round(width), round(height)),
             '<defs>'
             '<marker id="arw-%s" viewBox="0 0 10 10" refX="9" refY="5" '
             'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
             '<path d="M0 0 L10 5 L0 10 Z" class="arwfill"/></marker>'
             '<marker id="arwh-%s" viewBox="0 0 10 10" refX="9" refY="5" '
             'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
             '<path d="M0 0 L10 5 L0 10 Z" class="arwfill-hot"/></marker>'
             '</defs>' % (uid, uid)]

    for nid in pop:                              # 先画边再画点
        for b in blockers_of(nid):
            x0, y0 = xy(b)
            x1, y1 = xy(nid)
            hot = not _is_done(pop[b])
            # 边从「标签末尾」画到下一列圆点（不从圆点画起），
            # 否则同行链的线正好穿过标签文字，看起来像划线
            x0e = x0 + R + LABEL_PAD + _text_w(labels[b]) + 4
            mx = (x0e + x1) / 2
            parts.append(
                '<path class="dep%s" marker-end="url(#%s-%s)" '
                'd="M%.1f %.1f C%.1f %.1f %.1f %.1f %.1f %.1f"/>'
                % (" hot" if hot else "", "arwh" if hot else "arw", uid,
                   x0e, y0, mx, y0, mx, y1, x1 - R - 3, y1))
    for nid in pop:
        parts.append(_node_svg(pop[nid], nid in ghosts, labels[nid], xy(nid)))
    parts.append("</svg>")
    return "".join(parts)


def _label_of(node, is_ghost):
    title = node["title"]
    number = (node["meta"] or {}).get("number")
    if is_ghost and number:
        title = "#%s %s" % (number, title)
    return _clip(title)


def _node_svg(node, is_ghost, label, pos):
    x, y = pos
    if is_ghost:
        cls = "ghost"
        text_cls = ' class="ghosttx"'
    else:
        done = _is_done(node)
        doing = any(b.startswith("claimed:") for b in node["badges"])
        cls = "done" if done else ("doing" if doing else "todo")
        if "frontier" in node["badges"]:
            cls += " frontier"
        text_cls = ' class="strike"' if done else ""
    body = ('<circle class="%s" cx="%.1f" cy="%.1f" r="%.1f"/>'
            '<text x="%.1f" y="%.1f" text-anchor="start"%s>%s</text>'
            '<title>%s</title>'
            % (cls, x, y, R, x + R + LABEL_PAD, y + 4, text_cls, esc(label),
               esc(_tooltip(node))))
    if node["url"]:
        return '<a class="n" data-id="%s" href="%s">%s</a>' \
            % (esc(node["id"]), esc(node["url"]), body)
    return '<g class="n" data-id="%s">%s</g>' % (esc(node["id"]), body)


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
