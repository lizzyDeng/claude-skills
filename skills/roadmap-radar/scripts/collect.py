"""跑 source、合并 Node、标 orphan、体检。不认识任何具体 source。"""

import model
from model import BADGE_EMPTY, KIND_GOAL, KIND_GROUP

BADGE_ORPHAN = "orphan"
BADGE_UNMAPPED = "unmapped-status"

_MERGEABLE_SCALARS = ("url", "parent", "progress", "state")


def merge(nodes):
    """按 id 合并。先到者的非 None 字段赢，缺的由后到者补，badges/meta 取并集。

    这是跨 source 对齐 goal 的唯一机制：forge 发 goal:obj-3、github label
    obj-3 也发 goal:obj-3，于是同一个 goal 拿到两边的信息。
    """
    order = []
    by_id = {}
    for raw in model.copy_nodes(nodes):
        nid = raw["id"]
        if nid not in by_id:
            by_id[nid] = raw
            order.append(nid)
            continue
        cur = by_id[nid]
        for key in _MERGEABLE_SCALARS:
            if cur.get(key) is None and raw.get(key) is not None:
                cur[key] = raw[key]
        for badge in raw["badges"]:
            if badge not in cur["badges"]:
                cur["badges"].append(badge)
        for key, value in raw["meta"].items():
            cur["meta"].setdefault(key, value)
    return [by_id[i] for i in order]


def mark_orphans(nodes):
    """parent 指向不存在的 id → 断开并打 orphan，把原 parent 存进 meta 当证据。

    永不丢节点 —— 悄悄丢节点的进度条会撒谎。
    """
    out = model.copy_nodes(nodes)
    ids = {n["id"] for n in out}
    for n in out:
        parent = n["parent"]
        if parent is not None and parent not in ids:
            n["meta"]["missing_parent"] = parent
            n["parent"] = None
            if BADGE_ORPHAN not in n["badges"]:
                n["badges"].append(BADGE_ORPHAN)
    return out


def doctor(nodes):
    """只报告不修。修法是项目专属的，报告是通用的。"""
    marked = model.rollup(mark_orphans(nodes))
    ids = {n["id"] for n in nodes}
    return {
        "dangling_parents": [
            {"id": n["id"], "title": n["title"], "missing_parent": n["parent"]}
            for n in nodes
            if n["parent"] is not None and n["parent"] not in ids
        ],
        "orphans": [n for n in marked if BADGE_ORPHAN in n["badges"]],
        # 「没挂上」和「断链」是两回事：断链的 parent 指向了不存在的 id（有证据），
        # 没挂上的 parent 从出生就是 None —— 比如 issue 没打 goal label。两类都要报。
        "no_goal": [
            n for n in marked
            if n["kind"] != KIND_GOAL and n["parent"] is None
            and BADGE_ORPHAN not in n["badges"]
        ],
        "empty_groups": [
            n for n in marked
            if n["kind"] in (KIND_GOAL, KIND_GROUP) and BADGE_EMPTY in n["badges"]
        ],
        "unmapped_status": [n for n in marked if BADGE_UNMAPPED in n["badges"]],
    }


def build(config, ctx, registry):
    """config -> {"nodes": [...], "doctor": {...}}"""
    collected = []
    for source_cfg in config.get("sources", []):
        source_type = source_cfg.get("type")
        if source_type not in registry:
            raise ValueError(
                "unknown source type %r; known: %s" % (source_type, sorted(registry))
            )
        collected.extend(registry[source_type](source_cfg, ctx))
    merged = merge(collected)
    report = doctor(merged)
    nodes = model.rollup(mark_orphans(merged))
    return {"nodes": nodes, "doctor": report}
