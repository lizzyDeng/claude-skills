"""forge roadmap source —— 两个 reader（roadmap.json / roadmap.md），按存在性探测。

json 有 obj-N id 和 slug（能和 GitHub label 对上），md 三个项目格式统一但只有名字。
所以 json 优先、md 兜底。
"""

import json
import os
import re

import model

NORTH_STAR_ID = "forge:north-star"

# 三个项目共用一套状态词（json 的 status 字段和 md 的表格文字一致）
DEFAULT_STATUS_PROGRESS = {
    "draft": 0.0,
    "planned": 0.1,
    "in_progress": 0.5,
    "measuring": 0.85,
    "concluded": 1.0,
}

GOAL_FIELD_CANDIDATES = ("objective_id", "objective")
TITLE_FIELD_CANDIDATES = ("north_star", "project")

BADGE_UNMAPPED = "unmapped-status"


def collect(config, ctx):
    path = ctx.resolve(config.get("path") or "project-roadmap/roadmap.json")
    reader = _pick_reader(path)
    return reader(path, config)


def _pick_reader(path):
    """探测式分发：显式 .md 走 md reader；.json 存在走 json，否则退同目录 roadmap.md。"""
    if path.endswith(".md"):
        return _read_md
    if os.path.isfile(path):
        return _read_json
    fallback = os.path.join(os.path.dirname(path), "roadmap.md")
    if os.path.isfile(fallback):
        return lambda _p, cfg: _read_md(fallback, cfg)
    raise FileNotFoundError("no roadmap.json or roadmap.md near %s" % path)


def _status_table(config):
    table = dict(DEFAULT_STATUS_PROGRESS)
    table.update(config.get("status_progress") or {})
    return table


def _probe(candidates, has):
    for name in candidates:
        if has(name):
            return name
    return None


def _feature_node(slug, name, goal_raw, status, table, meta):
    progress = table.get(status)
    badges = [] if progress is not None else [BADGE_UNMAPPED]
    return model.node(
        id="forge:%s" % slug,
        kind=model.KIND_LEAF,
        title=name or slug,
        parent=("goal:%s" % goal_raw) if goal_raw else None,
        progress=progress,
        state=status,
        badges=badges,
        meta=meta,
    )


def _read_json(path, config):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    features = data.get("features", [])
    table = _status_table(config)

    goal_field = config.get("goal_field") or _probe(
        GOAL_FIELD_CANDIDATES, lambda k: any(k in f for f in features)
    )
    title_field = config.get("title_field") or _probe(
        TITLE_FIELD_CANDIDATES, lambda k: bool(data.get(k))
    )

    nodes = []
    banner = data.get(title_field) if title_field else None
    if banner and banner != "(undefined)":
        nodes.append(model.node(NORTH_STAR_ID, model.KIND_GOAL, banner,
                                meta={"source": "forge", "kind": "north-star"}))

    for objective in data.get("objectives", []):
        nodes.append(model.node(
            id="goal:%s" % objective["id"],
            kind=model.KIND_GOAL,
            title=objective.get("name") or objective["id"],
            parent=NORTH_STAR_ID if banner else None,
            meta={"source": "forge",
                  "target_metric": objective.get("target_metric"),
                  "description": objective.get("description")},
        ))

    for feature in features:
        nodes.append(_feature_node(
            slug=feature["slug"],
            name=feature.get("name"),
            goal_raw=feature.get(goal_field) if goal_field else None,
            status=feature.get("status"),
            table=table,
            meta={"source": "forge",
                  "shipped_at": feature.get("shipped_at"),
                  "harvest_due": feature.get("harvest_due")},
        ))

    nodes.extend(_orphan_feature_dirs(path, {f["slug"] for f in features}))
    return nodes


def _orphan_feature_dirs(roadmap_path, known_slugs):
    """features/ 里有目录但 roadmap 没登记 —— 必须出现在图里，不能悄悄丢。"""
    feature_dir = os.path.join(os.path.dirname(roadmap_path), "features")
    if not os.path.isdir(feature_dir):
        return []
    out = []
    for name in sorted(os.listdir(feature_dir)):
        if name in known_slugs or not os.path.isdir(os.path.join(feature_dir, name)):
            continue
        out.append(model.node(
            id="forge:%s" % name,
            kind=model.KIND_LEAF,
            title=name,
            badges=["orphan", model.BADGE_NO_COUNT],
            meta={"source": "forge",
                  "reason": "features/ 有目录但不在 roadmap 里"},
        ))
    return out


_NORTH_STAR_RE = re.compile(r"^>\s*North Star:\s*(.+?)\s*$")
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_TARGET_RE = re.compile(r"^Target:\s*(.+?)\s*$")
_SKIP_HEADINGS = {"Summary"}


def _read_md(path, config):
    """解析 forge 生成的 roadmap.md。三个项目格式一致：

        > North Star: <text>
        ## <objective 名>
        Target: <metric>
        | Feature | Status | Shipped | Harvest |
        |---------|--------|---------|---------|
        | 送礼物  | 📋 draft | - | - |

    md 没有 obj-N id，所以 goal id 用名字：goal:name:<名>。
    """
    table = _status_table(config)
    nodes = []
    banner = None
    current_goal = None

    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    for line in lines:
        star = _NORTH_STAR_RE.match(line)
        if star:
            value = star.group(1)
            if value != "(undefined)":
                banner = value
                nodes.append(model.node(NORTH_STAR_ID, model.KIND_GOAL, banner,
                                        meta={"source": "forge",
                                              "kind": "north-star"}))
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            name = heading.group(1)
            if name in _SKIP_HEADINGS:
                current_goal = None
                continue
            current_goal = "goal:name:%s" % name
            nodes.append(model.node(
                id=current_goal, kind=model.KIND_GOAL, title=name,
                parent=NORTH_STAR_ID if banner else None,
                meta={"source": "forge"}))
            continue

        target = _TARGET_RE.match(line)
        if target and current_goal:
            for existing in nodes:
                if existing["id"] == current_goal:
                    existing["meta"]["target_metric"] = target.group(1)
            continue

        row = _parse_table_row(line)
        if row and current_goal:
            name, status, shipped, harvest = row
            nodes.append(_feature_node(
                slug="name:%s" % name, name=name, goal_raw=None,
                status=status, table=table,
                meta={"source": "forge", "shipped_at": shipped,
                      "harvest_due": harvest}))
            nodes[-1]["parent"] = current_goal

    return nodes


def _parse_table_row(line):
    """`| 送礼物 | 📋 draft | - | - |` -> ("送礼物", "draft", None, None)。

    表头和分隔行返回 None。状态取末尾空格分词，因此 emoji 无关。
    """
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    cells = [c.strip() for c in stripped.strip("|").split("|")]
    if len(cells) < 2:
        return None
    if cells[0] in ("Feature", "") or set(cells[0]) <= set("-: "):
        return None
    status = cells[1].split()[-1] if cells[1] else None
    dash = lambda v: None if v in ("-", "") else v  # noqa: E731
    return cells[0], status, dash(cells[2] if len(cells) > 2 else ""), \
        dash(cells[3] if len(cells) > 3 else "")
