import html as html_mod
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2] / "skills" / "roadmap-radar" / "scripts"
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(BASE))

import render  # noqa: E402


def _plain(html):
    """归一 graphviz 输出：- 会转成 &#45;，混排文本拆成多个 <text> run。"""
    html = re.sub(r"</text>\s*<text[^>]*>", "", html)
    return (html.replace("&#45;", "-").replace("&#10;", "\n")
                .replace("&quot;", '"').replace("&#39;", "'")
                .replace("&gt;", ">"))


GRAPH = json.loads((FIX / "graph.json").read_text(encoding="utf-8"))
HTML = _plain(render.render(GRAPH))
REPO = "gh:hyoteam/aifriends"


def _block_containing(html, needle):
    """取包含 needle 的那个 graphviz 节点块（外层 <g id="node…>…</a>）。"""
    index = html.index(needle)
    start = html.rfind('<g id="node', 0, index)
    return html[start:html.index("</a>", index)]


def _cx(html, title, lane=None):
    """节点标签文字的 x 坐标（列位置随前置层数右移）。"""
    block = _block_containing(lane if lane is not None else html, title)
    match = re.search(r'<text[^>]*? x="([-\d.]+)"', block)
    assert match, title
    return float(match.group(1))


def _lane_html(html, needle):
    """取包含 needle 的那个 <section class="lane">…</section>。"""
    index = html.index(needle)
    start = html.rfind('<section class="lane">', 0, index)
    return html[start:html.index("</section>", index)]


def test_is_one_self_contained_document():
    assert HTML.lstrip().startswith("<!DOCTYPE html>")
    assert "<style>" in HTML
    assert "http://" not in HTML.split("<body")[0]      # head 里没有外链
    assert "<script src=" not in HTML


def test_each_containment_root_is_a_lane():
    assert HTML.count('<section class="lane">') >= 3    # 两张 map + 散票区
    assert "[map] her live-chat 实施路线" in HTML
    assert "[map] her 渠道「看广告换额度」" in HTML


def test_blocking_chain_layers_left_to_right():
    """29→32→34 是三层链：列坐标必须严格递增。"""
    lane = _lane_html(HTML, "live-chat 实施路线")
    x29 = _cx(HTML, "apimart 可用型号清单", lane)
    x32 = _cx(HTML, "sys_config 补 callers.chat", lane)
    x34 = _cx(HTML, "半双工语音 MVP", lane)
    assert x29 < x32 < x34


def test_edges_have_arrowheads_and_hot_marks_open_blockers():
    lane = _lane_html(HTML, "live-chat 实施路线")
    assert "<polygon" in lane                    # graphviz 原生箭头
    assert 'class="edge dep hot"' in lane        # 30 还 open，30→33 是卡住边
    assert 'class="edge dep"' in lane            # 29 已关，29→32 正常边


def test_dot_color_encodes_state():
    assert 'class="node done"' in _block_containing(HTML, "C 端接入")
    assert 'class="node doing"' in _block_containing(HTML, "ads SSV 服务端验证")
    block31 = _block_containing(HTML, "决策: live-chat 三个产品口径")
    assert 'class="node todo frontier"' in block31


def test_frontier_ring_survives_closed_blockers():
    """#32 的前置 #29 已关 → 可上手，蓝圈还在。"""
    assert 'class="node todo frontier"' in \
        _block_containing(HTML, "sys_config 补 callers.chat")


def test_out_of_lane_blocker_appears_as_ghost_dot():
    """#22 的前置 #29 在另一条泳道 → 本泳道画虚线幽灵点，带票号。"""
    lane = _lane_html(HTML, "看广告换额度")
    assert 'class="node ghost"' in lane
    assert "#29" in lane


def test_wrap_never_splits_latin_words():
    """断行不腰斩半角单词：aifriend 不能变成 aifrien|d。"""
    lines = render._wrap("[research] aifriend 耦合点盘点:换皮施工图全景与迁移顺序")
    for line in lines:
        assert not line.endswith("aifrien"), lines
    joined = "".join(lines).replace("…", "")
    assert "aifriend" in "".join(lines) or joined in "[research] aifriend 耦合点盘点:换皮施工图全景与迁移顺序"
    lines2 = render._wrap("sys_config 补 callers.chat —— 主聊天模型脱离 env 热更")
    assert not any(line.endswith("calle") for line in lines2), lines2


def test_wrap_caps_lines_and_appends_ellipsis():
    lines = render._wrap("很长的标题" * 20)
    assert len(lines) == render.MAX_LINES
    assert lines[-1].endswith("…")
    short = render._wrap("短标题")
    assert short == ["短标题"]


def test_lane_has_start_and_end_anchors():
    """每条泳道有唯一入口/出口：开始 → 无前置的票 … 末端票 → 完成。"""
    lane = _lane_html(HTML, "live-chat 实施路线")
    assert "开始" in lane
    assert "完成1/6" in lane            # #29 已关，其余 5 张 open（多行 label 归一后）
    assert "__start->" in lane          # 开始有出边
    assert "->__end" in lane            # 完成有入边


def test_end_anchor_green_only_when_all_done():
    graph = {"nodes": [
        {"id": "gh:r#1", "kind": "group", "title": "M", "url": None,
         "parent": None, "progress": 1.0, "state": "closed", "badges": [],
         "meta": {}},
        {"id": "gh:r#2", "kind": "leaf", "title": "T", "url": None,
         "parent": "gh:r#1", "progress": 1.0, "state": "closed",
         "badges": [], "meta": {}},
    ], "doctor": {}}
    out = _plain(render.render(graph))
    assert 'class="node anchor ok"' in out
    assert "完成1/1" in out
    lane = _lane_html(HTML, "live-chat 实施路线")     # 没做完的泳道不绿
    assert 'class="node anchor ok"' not in lane


def test_isolated_ticket_is_wired_through_start_and_end():
    """#25 没有任何依赖边：必须被 开始→#25→完成 串进主流，不悬空。"""
    lane = _lane_html(HTML, "看广告换额度")
    nid = "%s#25" % REPO
    assert "__start->%s" % nid in lane
    assert "%s->__end" % nid in lane


def test_stray_section_has_no_anchors():
    lane = _lane_html(HTML, "散票")
    assert "__start" not in lane
    assert "__end" not in lane


def test_strays_land_in_stray_lane_not_dropped():
    lane = _lane_html(HTML, "散票")
    assert "随便一个没挂地图的 bug" in lane
    assert "ghost-one" in lane
    assert "约会探针《周末在家》" in lane      # 空地图无子票，也落散票区


def test_goal_is_a_footnote_not_structure():
    """forge 已砍：goal 只是泳道 chip + 页脚一行，永远不是泳道/分区标题。"""
    tail = HTML.split('id="goals"')[1]
    assert "变现能力" in tail
    assert "<b>变现能力</b>" not in HTML           # 不是任何泳道的标题
    assert '<span class="tag">变现能力</span>' in HTML   # 只是 map 头上的小标签


def test_doctor_reports_dangling_and_empty():
    tail = HTML.split('id="doctor"')[1]
    assert "#404" in tail
    assert "约会探针《周末在家》" in tail


def test_tooltip_carries_summary_and_sections():
    block = _block_containing(HTML, "spike: 确认 apimart 可用型号清单")
    assert "是否透传非推理模型" in block                 # 一句话简介进 tooltip
    lane_head = _lane_html(HTML, "live-chat 实施路线")
    assert "决策 2" in lane_head                          # map 决策收计数进 tooltip
    assert "延迟地板 800ms" in lane_head


def test_every_node_with_url_is_a_link():
    assert 'href="https://github.com/hyoteam/aifriends/issues/22"' in HTML


def test_html_escapes_titles():
    graph = {"nodes": [{"id": "x", "kind": "leaf", "title": "<script>x</script>",
                        "url": None, "parent": None, "progress": 0.5, "state": None,
                        "badges": [], "meta": {}}],
             "doctor": {}}
    out = render.render(graph)
    assert "<script>x</script>" not in out.split("<body")[1]
    assert "&lt;script&gt;" in out


def test_renders_issue_only_graph_without_any_goal():
    graph = {"nodes": [
        {"id": "gh:r#1", "kind": "group", "title": "M", "url": None,
         "parent": None, "progress": 0.0, "state": "open", "badges": [],
         "meta": {}},
        {"id": "gh:r#2", "kind": "leaf", "title": "T", "url": None,
         "parent": "gh:r#1", "progress": 0.0, "state": "open",
         "badges": ["frontier"], "meta": {}},
    ], "doctor": {}}
    out = render.render(graph)
    assert "M" in out and "T" in out
    assert 'id="goals"' not in out


def test_no_node_is_ever_dropped_from_html():
    """契约：图里每个节点的标题都必须出现在 HTML 里（tooltip 兜底全文）。"""
    for node in GRAPH["nodes"]:
        assert html_mod.escape(node["title"], quote=False) in HTML, node["id"]


def test_dependency_cycle_does_not_hang_render():
    graph = {"nodes": [
        {"id": "gh:r#1", "kind": "leaf", "title": "A", "url": None, "parent": None,
         "progress": 0.0, "state": "open", "badges": [],
         "meta": {"blocked_by": ["gh:r#2"]}},
        {"id": "gh:r#2", "kind": "leaf", "title": "B", "url": None, "parent": None,
         "progress": 0.0, "state": "open", "badges": [],
         "meta": {"blocked_by": ["gh:r#1"]}},
    ], "doctor": {}}
    out = render.render(graph)
    assert "A" in out and "B" in out
