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

FS = 10.5          # 节点下方标题的字号（px），像素宽估算基准
LINE_W = 118       # 标题单行最大像素宽
MAX_LINES = 2      # 标题最多两行，超出截断加 …
LABEL_DY = 16      # 圆点中心到第一行标题的垂直距离
LINE_H = 13        # 标题行高
DOT_BIN = "dot"

CSS = """
/* 固定浅色主题：不跟随系统暗色（用户拍板，暗色太丑） */
:root { color-scheme: light; --fg:#111; --dim:#666; --line:#ddd; --edge:#a8a8a8;
        --bar:#3b82f6; --ok:#16a34a; --doing:#f59e0b; --todo:#8b9199;
        --warn:#dc2626; --card:#fff; --bg:#fafafa; }
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
.prio { display:inline-block; font-size:11px; font-weight:700; padding:0 7px;
        border-radius:9px; color:#fff; margin-right:8px; vertical-align:1px; }
.prio.p0 { background:var(--warn); }
.prio.p1 { background:var(--doing); }
.prio.p2 { background:var(--bar); }
.prio.pn { background:var(--todo); }

/* ---- graphviz SVG：几何来自 dot，颜色全在这里；标题由 _inject_labels
       画在圆点正下方（text.nlabel） ---- */
svg.dag { display:block; margin-top:4px; }
svg.dag text { fill:var(--fg); }
svg.dag text.nlabel { font:10.5px -apple-system,BlinkMacSystemFont,"Segoe UI",
                      "PingFang SC",sans-serif;
                      paint-order:stroke; stroke:var(--card); stroke-width:3px;
                      stroke-linejoin:round; }
svg.dag .node ellipse { fill:var(--card); stroke:var(--todo); stroke-width:1.5; }
svg.dag .node.todo ellipse { fill:var(--todo); fill-opacity:.25; }
svg.dag .node.doing ellipse { fill:var(--doing); stroke:var(--doing); }
svg.dag .node.done ellipse { fill:var(--ok); stroke:var(--ok); }
/* 已完成只靠绿点表达；标题划删除线会和穿行的边线叠成一团不可读 */
svg.dag .node.done text.nlabel { fill:var(--dim); }
svg.dag .node.frontier ellipse { stroke:var(--bar); stroke-width:2.4; }
svg.dag .node.ghost ellipse { fill:none; stroke:var(--dim);
                              stroke-dasharray:3 3; }
svg.dag .node.ghost text { fill:var(--dim); }
svg.dag .edge path { fill:none; stroke:var(--edge); stroke-width:1.3; }
svg.dag .edge polygon { fill:var(--edge); stroke:var(--edge); }
svg.dag .edge.hot path { stroke:var(--warn); }
svg.dag .edge.hot polygon { fill:var(--warn); stroke:var(--warn); }
svg.dag .edge.flow path { stroke:var(--line); }
svg.dag .edge.flow polygon { fill:var(--line); stroke:var(--line); }
svg.dag .node.anchor ellipse { fill:var(--card); stroke:var(--dim);
                               stroke-width:1.3; }
svg.dag .node.anchor text { fill:var(--dim); }
svg.dag .node.anchor.ok ellipse { fill:var(--ok); fill-opacity:.15;
                                  stroke:var(--ok); }
svg.dag .node.anchor.ok text { fill:var(--ok); }

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


def _is_cjk(ch):
    return ord(ch) >= 0x2E80


_BREAK_AFTER = set(" -—_:：/·,，+)）」』…")


def _wrap(text, line_w=LINE_W, max_lines=MAX_LINES):
    """标题 → 最多 max_lines 行，超出截断加 …。

    断行优先落在词边界（空格 / CJK 两侧 / 标点后），半角单词不腰斩 ——
    纯按像素宽硬切会把 aifriend 断成 aifrien|d。整行没有断点才硬切。
    """
    lines = []
    cur = ""
    brk = 0            # cur[:brk] 是最后一个合法断点前的内容
    for ch in text:
        if cur and _text_w(cur + ch) > line_w:
            if brk > 0:
                lines.append(cur[:brk].rstrip())
                cur = (cur[brk:] + ch).lstrip()
            else:
                lines.append(cur)
                cur = ch
            brk = 0
            for i in range(1, len(cur)):
                if (cur[i - 1] in _BREAK_AFTER or _is_cjk(cur[i - 1])
                        or _is_cjk(cur[i])):
                    brk = i
            if len(lines) == max_lines:
                break
        else:
            cur += ch
            if len(cur) > 1 and (cur[-2] in _BREAK_AFTER or _is_cjk(cur[-2])
                                 or _is_cjk(cur[-1])):
                brk = len(cur) - 1
    if len(lines) == max_lines:
        last = lines[-1]
        while last and _text_w(last + "…") > line_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
        return lines
    if cur:
        lines.append(cur)
    return lines


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
    # 泳道按优先级排（P0 最前）；没打 priority label 的排最后、保持原顺序
    lanes.sort(key=lambda n: ((n["meta"] or {}).get("priority")
                              if (n["meta"] or {}).get("priority") is not None
                              else 99))
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
    priority = (root["meta"] or {}).get("priority")
    badge = ""
    if priority is not None:
        cls = "p%d" % priority if priority <= 2 else "pn"
        badge = '<span class="prio %s">P%d</span>' % (cls, priority)
    out = ['<section class="lane">',
           '<div class="lane-head" title="%s">%s<b>%s</b>'
           '<span class="cnt">%d/%d</span>%s</div>'
           % (esc(_tooltip(root)), badge, _link(root), done, len(members), chips)]
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
            % (len(strays), _dag_svg(strays, by_id, anchors=False)))


# ---------------- DOT 生成 + graphviz 渲染 ----------------

def _dag_svg(members, by_id, anchors=True):
    ids = {m["id"] for m in members}
    ghosts = {}
    for m in members:
        for b in (m["meta"] or {}).get("blocked_by") or []:
            if b not in ids and b not in ghosts:
                ghosts[b] = by_id.get(b) or model.node(b, model.KIND_LEAF, b)
    labels = {m["id"]: _wrap(m["title"]) for m in members}
    labels.update({gid: _wrap(_ghost_label(g)) for gid, g in ghosts.items()})
    svg = _dot_to_svg(_to_dot(members, ghosts, anchors))
    return _inject_labels(svg, labels)


def _to_dot(members, ghosts, anchors=True):
    """成员 + 泳道外幽灵前置票 → DOT。布局交给 graphviz，颜色只打 class。

    节点是**空 label 的小圆**（标题不进盒，渲后由 _inject_labels 精确画在
    圆点正下方）；nodesep/ranksep 为下方两行标题留出空间。
    anchors=True 时合成「开始 ▸ … ▸ 完成」两个锚点：开始连所有无前置的
    节点、所有末端节点汇入完成 —— 一条泳道于是有唯一的入口和出口，
    完成节点带 done/total，全完成时变绿。散票区不加锚（本来就零散）。
    """
    ids = {m["id"] for m in members}

    out = ["digraph radar {",
           '  rankdir=LR; bgcolor="transparent";',
           '  pack=44; packmode="array_c1";',   # pack 值 = component 间距（pt），孤票的下方标签才不叠
           '  graph [nodesep=0.78, ranksep=1.85, margin=0.05];',
           '  node [shape=circle, label="", width=0.17, height=0.17,'
           ' fixedsize=true];',
           '  edge [arrowsize=0.7];']

    for m in members:
        out.append('  "%s" [class="%s", href="%s", tooltip="%s"];'
                   % (_dq(m["id"]), _node_class(m),
                      _dq(m["url"] or "#"), _dq(_tooltip(m))))
    for gid, ghost in ghosts.items():
        out.append('  "%s" [class="ghost", href="%s", tooltip="%s"];'
                   % (_dq(gid), _dq(ghost["url"] or "#"), _dq(_tooltip(ghost))))

    member_by_id = {m["id"]: m for m in members}
    known = ids | set(ghosts)
    has_in = set()
    has_out = set()
    for m in members:
        for b in (m["meta"] or {}).get("blocked_by") or []:
            if b not in known:
                continue
            blocker = member_by_id.get(b) or ghosts.get(b)
            hot = blocker is not None and not _is_done(blocker)
            out.append('  "%s" -> "%s" [class="dep%s"];'
                       % (_dq(b), _dq(m["id"]), " hot" if hot else ""))
            has_in.add(m["id"])
            has_out.add(b)

    if anchors and members:
        done_n = sum(1 for m in members if _is_done(m))
        all_done = done_n == len(members)
        out.append('  "__start" [label="开始", shape=circle, class="anchor",'
                   ' fixedsize=false, width=0.5, fontsize=10, margin=0,'
                   ' fontname="Helvetica,PingFang SC",'
                   ' tooltip="起点：从无前置的票开始"];')
        out.append('  "__end" [label="完成\\n%d/%d", shape=doublecircle,'
                   ' class="anchor%s", fixedsize=false, width=0.5,'
                   ' fontsize=10, margin=0, fontname="Helvetica,PingFang SC",'
                   ' tooltip="泳道完成度 %d/%d"];'
                   % (done_n, len(members), " ok" if all_done else "",
                      done_n, len(members)))
        for nid in list(ids | set(ghosts)):
            if nid not in has_in:      # 无前置：开始从这里发出（幽灵也串上）
                out.append('  "__start" -> "%s" [class="flow", arrowsize=0.6];'
                           % _dq(nid))
        for m in members:
            if m["id"] not in has_out:   # 末端：没人依赖它 → 汇入完成
                out.append('  "%s" -> "__end" [class="flow", arrowsize=0.6];'
                           % _dq(m["id"]))
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
    return title


_NODE_G_RE = None   # 延迟编译（模块头不 import re，这里补）


def _inject_labels(svg, labels):
    """把每个节点的标题画在圆点正下方（居中，最多两行）。

    graphviz 的 xlabel 位置不可控，所以标题不进 dot：渲完拿每个节点
    <g> 块里的 <title>（node id）和第一个 <ellipse> 的圆心，自己插 <text>。
    锚点（__start/__end）自带盒内 label，跳过。
    """
    import re
    pattern = re.compile(
        r'(<g id="node\d+" class="node[^"]*">\s*<title>([^<]*)</title>'
        r'.*?<ellipse[^>]*cx="([-\d.]+)"[^>]*cy="([-\d.]+)"[^>]*/>)',
        re.S)

    def _unescape(text):
        return (text.replace("&#45;", "-").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">"))

    def add_label(match):
        block, node_id = match.group(1), _unescape(match.group(2))
        lines = labels.get(node_id)
        if not lines:
            return block
        x, y = float(match.group(3)), float(match.group(4))
        texts = "".join(
            '<text class="nlabel" x="%.1f" y="%.1f" text-anchor="middle">%s'
            '</text>' % (x, y + LABEL_DY + i * LINE_H, esc(line))
            for i, line in enumerate(lines))
        return block + texts

    return _expand_viewbox(pattern.sub(add_label, svg))


def _expand_viewbox(svg, pad_x=58, pad_bottom=42):
    """注入的下方标题会超出 graphviz 算的画布：左右和底部扩边，防裁切。"""
    import re
    match = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"', svg)
    if not match:
        return svg
    minx, miny, width, height = (float(match.group(i)) for i in range(1, 5))
    new_vb = 'viewBox="%.2f %.2f %.2f %.2f"' % (
        minx - pad_x, miny, width + pad_x * 2, height + pad_bottom)
    svg = svg.replace(match.group(0), new_vb, 1)
    svg = re.sub(r'width="[\d.]+pt"',
                 'width="%dpt"' % round(width + pad_x * 2), svg, count=1)
    svg = re.sub(r'height="[\d.]+pt"',
                 'height="%dpt"' % round(height + pad_bottom), svg, count=1)
    return svg


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
