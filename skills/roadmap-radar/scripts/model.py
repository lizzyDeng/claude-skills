"""Domain-free node graph + progress rollup.

This module知道的全部东西就是「一棵有进度的树」。它不知道 GitHub、forge、
wayfinder 存在，也不知道任何状态词表 —— 状态到数值的映射是 source 的责任。
"""

KIND_GOAL = "goal"
KIND_GROUP = "group"
KIND_LEAF = "leaf"

# rollup 自动打的 badge
BADGE_EMPTY = "empty"
BADGE_NO_COUNT = "no-count"
BADGE_CYCLE = "cycle"


def node(id, kind, title, url=None, parent=None, progress=None,
         state=None, badges=None, meta=None):
    """构造一个 Node。source 只该通过这个函数产出节点。"""
    return {
        "id": id,
        "kind": kind,
        "title": title,
        "url": url,
        "parent": parent,
        "progress": progress,
        "state": state,
        "badges": list(badges or []),
        "meta": dict(meta or {}),
    }


def copy_nodes(nodes):
    """深拷贝到可安全改写的程度（badges/meta 不共享）。"""
    return [dict(n, badges=list(n["badges"]), meta=dict(n["meta"])) for n in nodes]


def children_map(nodes):
    out = {}
    for n in nodes:
        if n["parent"] is not None:
            out.setdefault(n["parent"], []).append(n)
    return out


def children_of(nodes, node_id):
    return [n for n in nodes if n["parent"] == node_id]


def roots(nodes):
    ids = {n["id"] for n in nodes}
    return [n for n in nodes if n["parent"] is None or n["parent"] not in ids]


def _add_badge(n, badge):
    if badge not in n["badges"]:
        n["badges"].append(badge)


def rollup(nodes):
    """自底向上填 group/goal 的 progress。纯函数：返回新列表，输入不动。

    规则：
    - leaf 的 progress 由 source 给定，原样透传（可以是 None = 未知，不当 0）
    - 非 leaf 的 progress = 子节点 progress 的**等权平均**，None 的子节点不进分母
    - 0 个可计数子节点的非 leaf → progress=None + badge empty/no-count，
      因此它既不显示假进度，也不把父级分母拉大
    - 环 → 打 cycle badge 并当作无子节点，不死循环
    """
    out = copy_nodes(nodes)
    kids = children_map(out)
    done = {}
    visiting = set()

    def resolve(n):
        nid = n["id"]
        if nid in done:
            return done[nid]
        if nid in visiting:
            _add_badge(n, BADGE_CYCLE)
            return None
        visiting.add(nid)
        if n["kind"] == KIND_LEAF:
            value = n["progress"]
        else:
            counted = [c for c in kids.get(nid, []) if BADGE_NO_COUNT not in c["badges"]]
            values = [v for v in (resolve(c) for c in counted) if v is not None]
            if values:
                value = sum(values) / len(values)
            else:
                _add_badge(n, BADGE_EMPTY)
                _add_badge(n, BADGE_NO_COUNT)
                value = None
            n["progress"] = value
        visiting.discard(nid)
        done[nid] = value
        return value

    for n in out:
        resolve(n)
    return out


def leaf_counts(nodes, node_id):
    """(done, total) —— 只数直接子 leaf 里 progress>=1 的，给 UI 显示 "2/4"。"""
    kids = children_of(nodes, node_id)
    total = [k for k in kids if BADGE_NO_COUNT not in k["badges"]]
    done = [k for k in total if (k["progress"] or 0) >= 1.0]
    return len(done), len(total)
