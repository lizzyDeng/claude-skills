import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2] / "skills" / "roadmap-radar" / "scripts"
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(BASE))

import render  # noqa: E402

GRAPH = json.loads((FIX / "graph.json").read_text(encoding="utf-8"))
HTML = render.render(GRAPH)


def test_is_one_self_contained_document():
    assert HTML.lstrip().startswith("<!DOCTYPE html>")
    assert "<style>" in HTML
    assert "http://" not in HTML.split("<body")[0]      # head 里没有外链
    assert "<script src=" not in HTML


def test_north_star_is_the_banner():
    head = HTML.split("</header>")[0]
    assert "日均使用 30 分钟" in head


def test_each_goal_renders_a_percentage():
    assert "12%" in HTML     # obj-3
    assert "100%" in HTML    # obj-7


def test_goal_target_metric_is_shown():
    assert "免费→付费转化率 &gt;= 8%" in HTML


def test_map_shows_done_over_total():
    assert "1/2" in HTML     # 看广告换额度：#25 closed / #22 open


def test_empty_map_is_flagged_not_shown_as_zero_percent():
    block = _block_containing(HTML, "约会探针《周末在家》")
    assert "0%" not in block
    assert "空" in block or "empty" in block


def test_frontier_ticket_is_marked():
    block = _block_containing(HTML, "ads SSV 服务端验证")
    assert "frontier" in block


def test_unassigned_section_lists_orphans_and_dangling_parents():
    tail = HTML.split('id="unassigned"')[1]
    assert "ghost-one" in tail
    assert "embedding 模型独立配 provider" in tail
    assert "#404" in tail          # 断链证据要显示出来


def test_every_node_with_url_is_a_link():
    assert 'href="https://github.com/hyoteam/aifriends/issues/22"' in HTML


def test_html_escapes_titles():
    graph = {"nodes": [{"id": "g", "kind": "goal", "title": "<script>x</script>",
                        "url": None, "parent": None, "progress": 0.5, "state": None,
                        "badges": [], "meta": {}}],
             "doctor": {"dangling_parents": [], "orphans": [], "empty_groups": [],
                        "unmapped_status": []}}
    out = render.render(graph)
    assert "<script>x</script>" not in out.split("<body")[1]
    assert "&lt;script&gt;" in out


def test_renders_without_north_star_root():
    """github-issues-only 项目没有北极星节点，也要出得来。"""
    graph = {"nodes": [
        {"id": "goal:obj-1", "kind": "goal", "title": "G", "url": None,
         "parent": None, "progress": 0.5, "state": None, "badges": [], "meta": {}},
        {"id": "gh:r#1", "kind": "leaf", "title": "T", "url": None,
         "parent": "goal:obj-1", "progress": 0.5, "state": "open",
         "badges": [], "meta": {}},
    ], "doctor": {"dangling_parents": [], "orphans": [], "empty_groups": [],
                  "unmapped_status": []}}
    out = render.render(graph)
    assert "50%" in out
    assert "G" in out


def _block_containing(html, needle):
    """取包含 needle 的那个 <details>/<li> 块，用于局部断言。"""
    index = html.index(needle)
    start = max(html.rfind("<details", 0, index), html.rfind("<li", 0, index))
    end = index + html[index:].find("</details>") if "</details>" in html[index:] \
        else len(html)
    return html[start:end]
