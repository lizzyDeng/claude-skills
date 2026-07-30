import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2] / "skills" / "roadmap-radar" / "scripts"
sys.path.insert(0, str(BASE))   # collect.py 内部 `import model`，必须先上 path

import collect as c  # noqa: E402
import model as m  # noqa: E402


def test_same_goal_id_from_two_sources_merges_into_one_node():
    forge_goal = m.node("goal:obj-3", m.KIND_GOAL, "变现能力",
                        meta={"target_metric": "转化率 >= 8%"})
    gh_goal = m.node("goal:obj-3", m.KIND_GOAL, "obj-3", url="https://x/labels/obj-3")
    out = c.merge([forge_goal, gh_goal])
    ids = [n["id"] for n in out]
    assert ids.count("goal:obj-3") == 1
    merged = out[0]
    assert merged["title"] == "变现能力"                       # 先到的标题赢
    assert merged["url"] == "https://x/labels/obj-3"           # 缺的字段由后到者补
    assert merged["meta"]["target_metric"] == "转化率 >= 8%"


def test_merge_unions_badges():
    a = m.node("x", m.KIND_LEAF, "x", badges=["frontier"])
    b = m.node("x", m.KIND_LEAF, "x", badges=["blocked"])
    out = c.merge([a, b])
    assert sorted(out[0]["badges"]) == ["blocked", "frontier"]


def test_dangling_parent_becomes_orphan_and_keeps_evidence():
    nodes = [m.node("t1", m.KIND_LEAF, "ticket", parent="gh:repo#999", progress=0.0)]
    out = c.mark_orphans(nodes)
    assert out[0]["parent"] is None
    assert "orphan" in out[0]["badges"]
    assert out[0]["meta"]["missing_parent"] == "gh:repo#999"


def test_valid_parent_is_untouched():
    nodes = [
        m.node("g", m.KIND_GROUP, "g"),
        m.node("t", m.KIND_LEAF, "t", parent="g", progress=0.0),
    ]
    out = {n["id"]: n for n in c.mark_orphans(nodes)}
    assert out["t"]["parent"] == "g"
    assert "orphan" not in out["t"]["badges"]


def test_no_node_is_ever_dropped():
    nodes = [
        m.node("a", m.KIND_LEAF, "a", parent="nope", progress=0.0),
        m.node("b", m.KIND_LEAF, "b", parent="nope2", progress=0.0),
    ]
    assert len(c.mark_orphans(nodes)) == 2


def test_doctor_reports_orphans_empty_groups_and_dangling_parents():
    nodes = [
        m.node("goal:obj-1", m.KIND_GOAL, "goal"),
        m.node("gh:r#1", m.KIND_GROUP, "empty map", parent="goal:obj-1"),
        m.node("gh:r#2", m.KIND_LEAF, "lost", parent="gh:r#404", progress=0.0),
        m.node("forge:x", m.KIND_LEAF, "unmapped", parent="goal:obj-1",
               progress=None, state="weird", badges=["unmapped-status"]),
    ]
    report = c.doctor(nodes)
    assert "gh:r#404" in report["dangling_parents"][0]["missing_parent"]
    assert "gh:r#1" in [n["id"] for n in report["empty_groups"]]
    assert "gh:r#2" in [n["id"] for n in report["orphans"]]
    assert "weird" in [n["state"] for n in report["unmapped_status"]]


def test_doctor_is_read_only():
    nodes = [m.node("a", m.KIND_LEAF, "a", parent="nope", progress=0.0)]
    c.doctor(nodes)
    assert nodes[0]["parent"] == "nope"        # 原始输入没被改
    assert nodes[0]["badges"] == []


def test_build_runs_sources_then_merges_then_marks_then_rolls_up():
    calls = []

    def fake_source(cfg, ctx):
        calls.append(cfg["type"])
        return [
            m.node("goal:obj-1", m.KIND_GOAL, "G"),
            m.node("forge:f", m.KIND_LEAF, "F", parent="goal:obj-1", progress=1.0),
        ]

    graph = c.build({"sources": [{"type": "fake"}]}, ctx=None,
                    registry={"fake": fake_source})
    assert calls == ["fake"]
    by_id = {n["id"]: n for n in graph["nodes"]}
    assert by_id["goal:obj-1"]["progress"] == 1.0
    assert graph["doctor"]["orphans"] == []


def test_build_raises_on_unknown_source_type_and_lists_known():
    try:
        c.build({"sources": [{"type": "nope"}]}, ctx=None, registry={"fake": lambda *a: []})
    except ValueError as exc:
        assert "nope" in str(exc) and "fake" in str(exc)
    else:
        raise AssertionError("expected ValueError")
