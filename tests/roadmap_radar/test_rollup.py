import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "roadmap-radar" / "scripts" / "model.py"


def load():
    spec = importlib.util.spec_from_file_location("radar_model", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = load()


def test_leaf_progress_passes_through():
    nodes = [m.node("l1", m.KIND_LEAF, "leaf", progress=0.5)]
    out = m.rollup(nodes)
    assert out[0]["progress"] == 0.5


def test_group_is_equal_weight_mean_of_children():
    nodes = [
        m.node("g", m.KIND_GROUP, "group"),
        m.node("a", m.KIND_LEAF, "a", parent="g", progress=1.0),
        m.node("b", m.KIND_LEAF, "b", parent="g", progress=0.0),
    ]
    out = {n["id"]: n for n in m.rollup(nodes)}
    assert out["g"]["progress"] == 0.5


def test_goal_weights_each_child_equally_regardless_of_subtree_size():
    """一张 6 票的 map 和一个 feature 在 goal 眼里各算一票。"""
    nodes = [
        m.node("goal", m.KIND_GOAL, "goal"),
        m.node("map", m.KIND_GROUP, "map", parent="goal"),
        m.node("feat", m.KIND_LEAF, "feat", parent="goal", progress=0.0),
    ]
    nodes += [m.node(f"t{i}", m.KIND_LEAF, f"t{i}", parent="map", progress=1.0) for i in range(6)]
    out = {n["id"]: n for n in m.rollup(nodes)}
    assert out["map"]["progress"] == 1.0
    assert out["goal"]["progress"] == 0.5  # (1.0 + 0.0) / 2，不是 6/7


def test_empty_group_gets_empty_and_no_count_and_is_excluded_from_parent():
    nodes = [
        m.node("goal", m.KIND_GOAL, "goal"),
        m.node("emptymap", m.KIND_GROUP, "empty", parent="goal"),
        m.node("feat", m.KIND_LEAF, "feat", parent="goal", progress=0.4),
    ]
    out = {n["id"]: n for n in m.rollup(nodes)}
    assert out["emptymap"]["progress"] is None
    assert "empty" in out["emptymap"]["badges"]
    assert "no-count" in out["emptymap"]["badges"]
    assert out["goal"]["progress"] == 0.4  # 空地图没把分母拉大


def test_source_declared_no_count_leaf_is_excluded():
    nodes = [
        m.node("goal", m.KIND_GOAL, "goal"),
        m.node("ok", m.KIND_LEAF, "ok", parent="goal", progress=1.0),
        m.node("orph", m.KIND_LEAF, "orph", parent="goal", progress=0.0, badges=["no-count"]),
    ]
    out = {n["id"]: n for n in m.rollup(nodes)}
    assert out["goal"]["progress"] == 1.0


def test_leaf_with_none_progress_does_not_fake_zero():
    """未映射状态的 leaf 不该被当成 0% 拉低 goal。"""
    nodes = [
        m.node("goal", m.KIND_GOAL, "goal"),
        m.node("known", m.KIND_LEAF, "known", parent="goal", progress=1.0),
        m.node("unknown", m.KIND_LEAF, "unknown", parent="goal", progress=None),
    ]
    out = {n["id"]: n for n in m.rollup(nodes)}
    assert out["goal"]["progress"] == 1.0


def test_nested_three_levels():
    nodes = [
        m.node("root", m.KIND_GOAL, "root"),
        m.node("g1", m.KIND_GROUP, "g1", parent="root"),
        m.node("g2", m.KIND_GROUP, "g2", parent="root"),
        m.node("a", m.KIND_LEAF, "a", parent="g1", progress=1.0),
        m.node("b", m.KIND_LEAF, "b", parent="g2", progress=0.0),
        m.node("c", m.KIND_LEAF, "c", parent="g2", progress=0.5),
    ]
    out = {n["id"]: n for n in m.rollup(nodes)}
    assert out["g2"]["progress"] == 0.25
    assert out["root"]["progress"] == 0.625


def test_cycle_is_badged_not_infinite_loop():
    nodes = [
        m.node("x", m.KIND_GROUP, "x", parent="y"),
        m.node("y", m.KIND_GROUP, "y", parent="x"),
    ]
    out = {n["id"]: n for n in m.rollup(nodes)}
    assert any("cycle" in n["badges"] for n in out.values())


def test_rollup_does_not_mutate_input():
    nodes = [
        m.node("g", m.KIND_GROUP, "g"),
        m.node("a", m.KIND_LEAF, "a", parent="g", progress=1.0),
    ]
    m.rollup(nodes)
    assert nodes[0]["progress"] is None
    assert nodes[0]["badges"] == []


def test_children_of_and_roots():
    nodes = [
        m.node("g", m.KIND_GROUP, "g"),
        m.node("a", m.KIND_LEAF, "a", parent="g", progress=1.0),
    ]
    assert [n["id"] for n in m.roots(nodes)] == ["g"]
    assert [n["id"] for n in m.children_of(nodes, "g")] == ["a"]
