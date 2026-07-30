# roadmap-radar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一条命令把 forge roadmap + wayfinder GitHub issue 合成一张自包含 HTML，显示每个 goal 的整体进度、当前能动的票（frontier）、以及没归位的东西。

**Architecture:** 域无关核心（Node 模型 + `rollup` 纯函数 + `render`）+ source 注册表（`forge` / `github-issues` / `wayfinder`）。核心完全不知道 forge/GitHub 存在，只见 Node 树。项目侧只加一个 `.radar.json`，零代码。

**Tech Stack:** Python 3 标准库 + `gh` CLI。零第三方依赖。HTML 内联 CSS、用原生 `<details>` 做展开（零 JS）。测试用 pytest。

**Spec:** `docs/superpowers/specs/2026-07-30-roadmap-radar-design.md`

## 与 spec 的两处偏离（已确认）

1. **`forge-json` + `forge-md` 合并成一个 `forge` source**。实测三个项目的 `roadmap.md` 格式完全一致（forge 生成），而 `roadmap.json` 字段名三处漂移、heartstory 根本没有。所以 md 是通用面、json 是补 `obj-N` id 的增强。一个 source 内含两个 reader，按文件存在性探测。四个 source → 三个。
2. **测试放仓库根 `tests/roadmap_radar/`**，不放 skill 目录内 —— 跟随 claude-skills 现有惯例（`tests/fastship/`、`tests/forge/`）。

---

## File Structure

```
skills/roadmap-radar/
  SKILL.md                      # 触发词 + 用法
  scripts/
    model.py                    # Node 构造 + rollup 纯函数（域无关，零 import 业务）
    collect.py                  # 跑 source + 合并去重 + 标 orphan + doctor 检查
    render.py                   # Node[] -> 自包含 HTML（域无关）
    radar.py                    # CLI 入口
    sources/
      __init__.py               # REGISTRY
      forge.py                  # roadmap.json / roadmap.md 双 reader
      github_issues.py          # 通用 GitHub：label/milestone 当 goal，sub-issue 当层级
      wayfinder.py              # github_issues 的 preset + map 正文分节解析
tests/roadmap_radar/
  fixtures/
    aifriends_roadmap.json      # 真实抽样（objective_id + north_star）
    supersdk_roadmap.json       # 真实抽样（objective + project）—— 验证字段探测
    heartstory_roadmap.md       # 真实抽样（无 json，只有 md）
    gh_issues.json              # 录制的 gh issue list 输出
    gh_sub_issues_12.json       # 录制的 sub_issues 输出
    graph.json                  # 渲染用固定输入
  test_rollup.py
  test_collect.py
  test_forge.py
  test_github_issues.py
  test_wayfinder.py
  test_render.py
```

责任边界：`model.py` 只做树和数；`collect.py` 只做合并与体检；`render.py` 只做 HTML；每个 source 只把自己那摊数据翻译成 Node。任何一个都能单独读懂、单独测。

---

### Task 1: Node 模型 + rollup 纯函数

核心的全部"聪明"都在这里。域无关，零业务 import。

**Files:**
- Create: `skills/roadmap-radar/scripts/model.py`
- Test: `tests/roadmap_radar/test_rollup.py`

- [ ] **Step 1: 写失败测试**

Create `tests/roadmap_radar/test_rollup.py`:

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/apple/works/claude-skills/.claude/worktrees/roadmap-radar && python3 -m pytest tests/roadmap_radar/test_rollup.py -v`
Expected: FAIL — `FileNotFoundError` / `No module named` （`model.py` 还不存在）

- [ ] **Step 3: 写 `model.py`**

Create `skills/roadmap-radar/scripts/model.py`:

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/roadmap_radar/test_rollup.py -v`
Expected: PASS，10 passed

- [ ] **Step 5: 提交**

```bash
git add skills/roadmap-radar/scripts/model.py tests/roadmap_radar/test_rollup.py
git commit -m "feat(roadmap-radar): 域无关 Node 模型 + rollup 纯函数

等权平均、空组不拉大分母、None 不当 0、环不死循环。"
```

---

### Task 2: collect.py — 合并、去重、标 orphan、doctor

source 之间靠**统一的 goal id** 对齐（`goal:<raw>`）。forge 发 `goal:obj-3`，github label `obj-3` 也发 `goal:obj-3`，同 id 就合成一个节点 —— 这正是"goal 整体进度"能跨两个数据源的原因。

**Files:**
- Create: `skills/roadmap-radar/scripts/collect.py`
- Test: `tests/roadmap_radar/test_collect.py`

- [ ] **Step 1: 写失败测试**

Create `tests/roadmap_radar/test_collect.py`:

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/roadmap_radar/test_collect.py -v`
Expected: FAIL — `collect.py` 不存在

- [ ] **Step 3: 写 `collect.py`**

Create `skills/roadmap-radar/scripts/collect.py`:

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/roadmap_radar/test_collect.py -v`
Expected: PASS，9 passed

- [ ] **Step 5: 提交**

```bash
git add skills/roadmap-radar/scripts/collect.py tests/roadmap_radar/test_collect.py
git commit -m "feat(roadmap-radar): collect —— 跨 source 合并/标 orphan/doctor

goal id 统一成 goal:<raw>,forge 和 github label 同 id 即合并,
这是 goal 进度能跨两个数据源的机制。永不丢节点。"
```

---

### Task 3: `forge` source — roadmap.json reader

字段探测让 aifriends（`objective_id`/`north_star`）和 supersdk（`objective`/`project`）都零配置命中。

**Files:**
- Create: `skills/roadmap-radar/scripts/sources/__init__.py`
- Create: `skills/roadmap-radar/scripts/sources/forge.py`
- Create: `tests/roadmap_radar/fixtures/aifriends_roadmap.json`
- Create: `tests/roadmap_radar/fixtures/supersdk_roadmap.json`
- Test: `tests/roadmap_radar/test_forge.py`

- [ ] **Step 1: 落两份真实 fixture**

Create `tests/roadmap_radar/fixtures/aifriends_roadmap.json`:

```json
{
  "north_star": "成为用户日均使用 30 分钟的 AI 情感陪伴平台",
  "created_at": "2026-05-07",
  "objectives": [
    {"id": "obj-1", "name": "对话粘性", "description": "用户愿意持续与 AI 聊天",
     "target_metric": "日均会话时长 >= 30min，次日留存 >= 40%"},
    {"id": "obj-3", "name": "变现能力", "description": "免费用户转付费",
     "target_metric": "免费→付费转化率 >= 8%，月付费留存 >= 70%"},
    {"id": "obj-7", "name": "引擎可演进性 / 工程健康", "description": "核心对话引擎可维护",
     "target_metric": "核心引擎无 >300 LOC 巨石函数"}
  ],
  "features": [
    {"slug": "gift-system", "name": "Gift System", "objective_id": "obj-1",
     "status": "concluded", "created_at": "2026-05-02", "shipped_at": "2026-05-14",
     "concluded_at": "2026-05-28"},
    {"slug": "core-tracking-events", "name": "Core Tracking Events",
     "objective_id": "obj-1", "status": "draft", "created_at": "2026-05-10"},
    {"slug": "weekend-date-foundation", "name": "Weekend Date Foundation",
     "objective_id": "obj-3", "status": "draft", "created_at": "2026-07-08"},
    {"slug": "video-generation", "name": "Video Generation",
     "objective_id": "obj-3", "status": "draft", "created_at": "2026-07-08"},
    {"slug": "chat-route-service-extraction", "name": "Chat Route Service Extraction",
     "objective_id": "obj-7", "status": "concluded", "created_at": "2026-06-20",
     "shipped_at": "2026-06-25", "concluded_at": "2026-07-02"},
    {"slug": "her-world-gift-wall", "name": "F1 礼物墙", "objective_id": "obj-6",
     "status": "measuring", "created_at": "2026-06-01", "shipped_at": "2026-07-10",
     "harvest_due": "2026-07-24"},
    {"slug": "mystery-status", "name": "Mystery Status", "objective_id": "obj-1",
     "status": "totally-new-word", "created_at": "2026-07-30"}
  ]
}
```

Create `tests/roadmap_radar/fixtures/supersdk_roadmap.json`:

```json
{
  "project": "让宿主 App 通过一个 SDK 获得 WebView 容器 + 登录/支付/广告能力",
  "created_at": "2026-05-16",
  "objectives": [
    {"id": "obj-1", "name": "用户体系",
     "target_metric": "Demo App 登录+支付全流程走通率 = 100%"},
    {"id": "obj-2", "name": "变现体系",
     "target_metric": "支持广告类型数 = 3（Banner + Interstitial + Reward）"}
  ],
  "features": [
    {"slug": "login-bridge", "name": "登录 Bridge", "objective": "obj-1",
     "status": "measuring", "created_at": "2026-05-16", "shipped_at": "2026-05-19",
     "harvest_due": "2026-05-26"},
    {"slug": "pay-bridge", "name": "支付 Bridge", "objective": "obj-1",
     "status": "draft", "created_at": "2026-05-16"},
    {"slug": "ad-bridge", "name": "广告 Bridge", "objective": "obj-2",
     "status": "draft", "created_at": "2026-05-16"}
  ]
}
```

- [ ] **Step 2: 写失败测试**

Create `tests/roadmap_radar/test_forge.py`:

```python
import importlib.util
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2] / "skills" / "roadmap-radar" / "scripts"
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(BASE))

import model  # noqa: E402
from sources import forge  # noqa: E402


class Ctx:
    def __init__(self, root):
        self.root = Path(root)

    def resolve(self, rel):
        return str(self.root / rel)

    def gh_json(self, args):  # forge source 不该用到
        raise AssertionError("forge source must not call gh")


def collect(fixture_name, **cfg):
    cfg.setdefault("path", fixture_name)
    return forge.collect(dict(cfg, type="forge"), Ctx(FIX))


def test_aifriends_probes_objective_id_and_north_star():
    nodes = {n["id"]: n for n in collect("aifriends_roadmap.json")}
    assert nodes["goal:obj-3"]["title"] == "变现能力"
    assert nodes["goal:obj-3"]["meta"]["target_metric"].startswith("免费→付费转化率")
    assert nodes["forge:weekend-date-foundation"]["parent"] == "goal:obj-3"
    banner = nodes["forge:north-star"]
    assert banner["kind"] == model.KIND_GOAL
    assert "30 分钟" in banner["title"]
    assert nodes["goal:obj-3"]["parent"] == "forge:north-star"


def test_supersdk_probes_objective_and_project_without_config():
    """字段名和 aifriends 不同，但零配置也要命中。"""
    nodes = {n["id"]: n for n in collect("supersdk_roadmap.json")}
    assert nodes["forge:login-bridge"]["parent"] == "goal:obj-1"
    assert nodes["forge:ad-bridge"]["parent"] == "goal:obj-2"
    assert "WebView" in nodes["forge:north-star"]["title"]


def test_status_maps_to_progress():
    nodes = {n["id"]: n for n in collect("aifriends_roadmap.json")}
    assert nodes["forge:gift-system"]["progress"] == 1.0        # concluded
    assert nodes["forge:core-tracking-events"]["progress"] == 0.0  # draft
    assert nodes["forge:her-world-gift-wall"]["progress"] == 0.85  # measuring


def test_unknown_status_is_none_and_badged_not_crash():
    nodes = {n["id"]: n for n in collect("aifriends_roadmap.json")}
    weird = nodes["forge:mystery-status"]
    assert weird["progress"] is None
    assert "unmapped-status" in weird["badges"]
    assert weird["state"] == "totally-new-word"


def test_status_progress_override_from_config():
    nodes = {n["id"]: n for n in collect("aifriends_roadmap.json",
                                        status_progress={"draft": 0.25})}
    assert nodes["forge:core-tracking-events"]["progress"] == 0.25
    assert nodes["forge:gift-system"]["progress"] == 1.0   # 未覆盖的词保持默认


def test_feature_pointing_at_absent_objective_keeps_parent_for_doctor():
    """obj-6 不在 objectives 列表里 —— 保留 parent，让 collect 标 orphan 报出来。"""
    nodes = {n["id"]: n for n in collect("aifriends_roadmap.json")}
    assert nodes["forge:her-world-gift-wall"]["parent"] == "goal:obj-6"
    assert "goal:obj-6" not in nodes


def test_orphan_feature_dirs_are_emitted(tmp_path):
    """features/ 里有目录但 roadmap.json 没登记 → 必须出现在图里。"""
    (tmp_path / "project-roadmap").mkdir()
    (tmp_path / "project-roadmap" / "roadmap.json").write_text(json.dumps({
        "north_star": "NS",
        "objectives": [{"id": "obj-1", "name": "G", "target_metric": "M"}],
        "features": [{"slug": "known", "name": "Known", "objective_id": "obj-1",
                      "status": "draft"}],
    }), encoding="utf-8")
    feats = tmp_path / "project-roadmap" / "features"
    (feats / "known").mkdir(parents=True)
    (feats / "ghost-one").mkdir()
    (feats / "ghost-two").mkdir()

    nodes = {n["id"]: n for n in forge.collect(
        {"type": "forge", "path": "project-roadmap/roadmap.json"}, Ctx(tmp_path))}
    assert "forge:ghost-one" in nodes
    assert "orphan" in nodes["forge:ghost-one"]["badges"]
    assert "no-count" in nodes["forge:ghost-one"]["badges"]
    assert "forge:ghost-two" in nodes


def test_registry_exposes_forge():
    from sources import REGISTRY
    assert "forge" in REGISTRY
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python3 -m pytest tests/roadmap_radar/test_forge.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sources'`

- [ ] **Step 4: 写 `sources/__init__.py`**

Create `skills/roadmap-radar/scripts/sources/__init__.py`:

```python
"""Source 注册表。新增 source 只加一行，不碰任何 if-else。"""

from . import forge, github_issues, wayfinder

REGISTRY = {
    "forge": forge.collect,
    "github-issues": github_issues.collect,
    "wayfinder": wayfinder.collect,
}
```

（`github_issues` / `wayfinder` 在 Task 5/6 建；本任务先建成只含 `collect` 的空壳，让 import 通得过。）

Create `skills/roadmap-radar/scripts/sources/github_issues.py`（本任务只放占位骨架，Task 5 实现）:

```python
"""通用 GitHub source。Task 5 实现。"""


def collect(config, ctx):
    raise NotImplementedError("implemented in Task 5")
```

Create `skills/roadmap-radar/scripts/sources/wayfinder.py`（Task 6 实现）:

```python
"""wayfinder preset。Task 6 实现。"""


def collect(config, ctx):
    raise NotImplementedError("implemented in Task 6")
```

- [ ] **Step 5: 写 `sources/forge.py` 的 json reader**

Create `skills/roadmap-radar/scripts/sources/forge.py`:

```python
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
```

- [ ] **Step 6: 跑测试确认通过**

Run: `python3 -m pytest tests/roadmap_radar/test_forge.py -v`
Expected: PASS，8 passed

- [ ] **Step 7: 提交**

```bash
git add skills/roadmap-radar/scripts/sources/ tests/roadmap_radar/test_forge.py \
        tests/roadmap_radar/fixtures/aifriends_roadmap.json \
        tests/roadmap_radar/fixtures/supersdk_roadmap.json
git commit -m "feat(roadmap-radar): forge source —— json/md 双 reader + 字段探测

aifriends(objective_id/north_star) 和 supersdk(objective/project)
零配置都命中。未映射状态返回 None 并打 badge,不崩不假装 0%。
features/ 里未登记的目录也进图。"
```

---

### Task 4: `forge` md reader 跑真实 heartstory 数据

Task 3 已把 md reader 写完，这个任务用**第三个项目的真数据**证明它 —— heartstory 是唯一没有 `roadmap.json` 的项目，也是 md reader 存在的唯一理由。

**Files:**
- Create: `tests/roadmap_radar/fixtures/heartstory_roadmap.md`
- Modify: `tests/roadmap_radar/test_forge.py`（追加测试）

- [ ] **Step 1: 落 heartstory 真实 fixture**

Create `tests/roadmap_radar/fixtures/heartstory_roadmap.md`:

```markdown
# Project Roadmap

> North Star: (undefined)

## 他认识我
Target: 每周角色主动引用记忆/兑现约定的消息数 >= 5 条

| Feature | Status | Shipped | Harvest |
|---------|--------|---------|---------|
| 生图角色形象一致性 | 🔄 in_progress | - | - |
| Telegram 用户身份稳定绑定 | 📋 draft | - | - |

## 他有自己的世界
Target: 用户因主动消息打开 app 的比例（push 打开率）>= 40%

| Feature | Status | Shipped | Harvest |
|---------|--------|---------|---------|
| AI 伴侣生图发图 | ⏳ measuring | 05-07 | Due 05-14 |
| Persona Engine SDK 抽取 | 🔄 in_progress | - | - |

## 我们之间有独特的东西
Target: 30 天留存率 >= 30%

| Feature | Status | Shipped | Harvest |
|---------|--------|---------|---------|

## Summary
📋 draft: 1 | 🔄 in_progress: 2 | ⏳ measuring: 1
```

- [ ] **Step 2: 追加失败测试**

Append to `tests/roadmap_radar/test_forge.py`:

```python
def test_md_reader_parses_heartstory_real_file():
    nodes = {n["id"]: n for n in collect("heartstory_roadmap.md")}
    goal = nodes["goal:name:他认识我"]
    assert goal["meta"]["target_metric"].startswith("每周角色主动引用记忆")
    assert nodes["forge:name:生图角色形象一致性"]["progress"] == 0.5   # in_progress
    assert nodes["forge:name:生图角色形象一致性"]["parent"] == "goal:name:他认识我"
    assert nodes["forge:name:AI 伴侣生图发图"]["progress"] == 0.85     # measuring
    assert nodes["forge:name:AI 伴侣生图发图"]["meta"]["shipped_at"] == "05-07"


def test_md_reader_skips_summary_heading_and_header_rows():
    ids = {n["id"] for n in collect("heartstory_roadmap.md")}
    assert "goal:name:Summary" not in ids
    assert not any(i.startswith("forge:name:Feature") for i in ids)
    assert not any(set(i.split("forge:name:")[-1]) <= set("-: ") for i in ids
                   if i.startswith("forge:name:"))


def test_md_reader_emits_no_north_star_when_undefined():
    ids = {n["id"] for n in collect("heartstory_roadmap.md")}
    assert "forge:north-star" not in ids   # "(undefined)" 不算北极星


def test_md_goal_with_empty_table_survives_as_empty_group():
    nodes = {n["id"]: n for n in collect("heartstory_roadmap.md")}
    lonely = nodes["goal:name:我们之间有独特的东西"]
    assert lonely["kind"] == model.KIND_GOAL
    rolled = {n["id"]: n for n in model.rollup(list(nodes.values()))}
    assert rolled[lonely["id"]]["progress"] is None
    assert "empty" in rolled[lonely["id"]]["badges"]


def test_json_missing_falls_back_to_sibling_md(tmp_path):
    (tmp_path / "project-roadmap").mkdir()
    (tmp_path / "project-roadmap" / "roadmap.md").write_text(
        "# Project Roadmap\n\n> North Star: NS\n\n## G\nTarget: T\n\n"
        "| Feature | Status | Shipped | Harvest |\n|---|---|---|---|\n"
        "| F | ✅ concluded | 05-01 | achieved |\n",
        encoding="utf-8")
    nodes = {n["id"]: n for n in forge.collect(
        {"type": "forge", "path": "project-roadmap/roadmap.json"}, Ctx(tmp_path))}
    assert nodes["forge:name:F"]["progress"] == 1.0
    assert nodes["forge:north-star"]["title"] == "NS"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python3 -m pytest tests/roadmap_radar/test_forge.py -k md -v`
Expected: FAIL —— fixture 刚建、断言未验证过；任何 `_parse_table_row` / `_read_md` 的偏差在这里暴露

- [ ] **Step 4: 修到通过**

改 `sources/forge.py` 的 `_read_md` / `_parse_table_row` 直到全绿。**不要改测试来迁就实现** —— fixture 是从真文件抄的，断言就是契约。

Run: `python3 -m pytest tests/roadmap_radar/test_forge.py -v`
Expected: PASS，13 passed

- [ ] **Step 5: 提交**

```bash
git add tests/roadmap_radar/fixtures/heartstory_roadmap.md \
        tests/roadmap_radar/test_forge.py skills/roadmap-radar/scripts/sources/forge.py
git commit -m "test(roadmap-radar): md reader 跑 heartstory 真实文件

heartstory 是唯一没有 roadmap.json 的项目,也是 md reader 存在的理由。
覆盖 (undefined) 北极星、Summary 跳过、空表格 goal、json 缺失退 md。"
```

---

### Task 5: `github-issues` source

通用层：任何 GitHub 仓，label 或 milestone 当 goal，sub-issue 当层级。`gh` 调用走 `ctx.gh_json` 注入，测试零网络。

**Files:**
- Modify: `skills/roadmap-radar/scripts/sources/github_issues.py`（替换占位）
- Create: `tests/roadmap_radar/fixtures/gh_issues.json`
- Create: `tests/roadmap_radar/fixtures/gh_sub_issues_12.json`
- Test: `tests/roadmap_radar/test_github_issues.py`

- [ ] **Step 1: 落录制的 gh fixture**

Create `tests/roadmap_radar/fixtures/gh_issues.json`（结构照 `gh issue list --json` 真实输出裁剪）:

```json
[
  {"number": 12, "title": "[wayfinder:map] persona-engine 借鉴北极星(polaris)增强",
   "state": "OPEN", "url": "https://github.com/hyoteam/aifriends/issues/12",
   "body": "## Destination\n4 个借鉴点全部定型\n\n## Decisions so far\n\n- 拆两轨 5 ticket,零丢弃\n- 抗漂移字段手填为准\n\n## Not yet specified\n\n- 预算感知分层的分档阈值\n\n## Out of scope\n\n- 换记忆算法(北极星弱于我们)\n",
   "labels": [{"name": "wayfinder:map"}, {"name": "obj-2"}],
   "assignees": [], "milestone": null},
  {"number": 13, "title": "预算感知人设分层（借鉴点#4）",
   "state": "OPEN", "url": "https://github.com/hyoteam/aifriends/issues/13",
   "body": "## Question\n分层阈值怎么定", "labels": [{"name": "wayfinder:task"}],
   "assignees": [], "milestone": null},
  {"number": 25, "title": "Feature A 记忆溯源",
   "state": "CLOSED", "url": "https://github.com/hyoteam/aifriends/issues/25",
   "body": "原票 okteam99/aifriends#534", "labels": [{"name": "wayfinder:task"}],
   "assignees": [], "milestone": null},
  {"number": 14, "title": "[wayfinder:map] her 渠道「看广告换额度」",
   "state": "OPEN", "url": "https://github.com/hyoteam/aifriends/issues/14",
   "body": "## Destination\n激励视频 → credits",
   "labels": [{"name": "wayfinder:map"}, {"name": "obj-3"}],
   "assignees": [], "milestone": null},
  {"number": 22, "title": "ads SSV 服务端验证",
   "state": "OPEN", "url": "https://github.com/hyoteam/aifriends/issues/22",
   "body": "## Question\nAdMob 回调端点", "labels": [{"name": "wayfinder:task"}],
   "assignees": [{"login": "lizzydeng"}], "milestone": null},
  {"number": 16, "title": "embedding 模型独立配 provider",
   "state": "OPEN", "url": "https://github.com/hyoteam/aifriends/issues/16",
   "body": "## Question\n向量空间迁移", "labels": [{"name": "wayfinder:task"}],
   "assignees": [], "milestone": null},
  {"number": 99, "title": "随便一个没打 obj label 的 bug",
   "state": "OPEN", "url": "https://github.com/hyoteam/aifriends/issues/99",
   "body": "boom", "labels": [{"name": "bug"}], "assignees": [], "milestone": null}
]
```

Create `tests/roadmap_radar/fixtures/gh_sub_issues_12.json`:

```json
[
  {"number": 13, "state": "open", "title": "预算感知人设分层（借鉴点#4）"},
  {"number": 25, "state": "closed", "title": "Feature A 记忆溯源"}
]
```

- [ ] **Step 2: 写失败测试**

Create `tests/roadmap_radar/test_github_issues.py`:

```python
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2] / "skills" / "roadmap-radar" / "scripts"
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(BASE))

import model  # noqa: E402
from sources import github_issues  # noqa: E402

REPO = "hyoteam/aifriends"


class FakeCtx:
    """录制回放：把 gh 调用按 args 特征映射到 fixture。"""

    def __init__(self, deps=None):
        self.deps = deps or {}
        self.calls = []

    def resolve(self, rel):
        return rel

    def gh_json(self, args):
        self.calls.append(args)
        joined = " ".join(args)
        if "issue" in args and "list" in args:
            return json.loads((FIX / "gh_issues.json").read_text(encoding="utf-8"))
        if "/sub_issues" in joined:
            number = joined.rsplit("/issues/", 1)[1].split("/")[0]
            path = FIX / ("gh_sub_issues_%s.json" % number)
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        if "/issues/" in joined:
            number = int(joined.rsplit("/issues/", 1)[1])
            return {"number": number,
                    "issue_dependencies_summary": {
                        "blocked_by": self.deps.get(number, 0)}}
        raise AssertionError("unexpected gh call: %r" % (args,))


def collect(ctx=None, **cfg):
    cfg.setdefault("repo", REPO)
    cfg.setdefault("goal_from", "label:^obj-\\d+$")
    cfg.setdefault("group_from", "label:^wayfinder:map$")
    return github_issues.collect(dict(cfg, type="github-issues"), ctx or FakeCtx())


def test_label_becomes_goal_node_with_canonical_id():
    nodes = {n["id"]: n for n in collect()}
    assert nodes["goal:obj-2"]["kind"] == model.KIND_GOAL
    assert nodes["goal:obj-3"]["kind"] == model.KIND_GOAL


def test_map_label_becomes_group_parented_to_its_goal():
    nodes = {n["id"]: n for n in collect()}
    map12 = nodes["gh:%s#12" % REPO]
    assert map12["kind"] == model.KIND_GROUP
    assert map12["parent"] == "goal:obj-2"
    assert map12["url"].endswith("/issues/12")


def test_sub_issues_become_leaves_under_the_map():
    nodes = {n["id"]: n for n in collect()}
    assert nodes["gh:%s#13" % REPO]["parent"] == "gh:%s#12" % REPO
    assert nodes["gh:%s#25" % REPO]["parent"] == "gh:%s#12" % REPO


def test_issue_state_maps_to_binary_progress():
    nodes = {n["id"]: n for n in collect()}
    assert nodes["gh:%s#25" % REPO]["progress"] == 1.0   # CLOSED
    assert nodes["gh:%s#13" % REPO]["progress"] == 0.0   # OPEN


def test_frontier_badge_needs_open_unassigned_unblocked():
    nodes = {n["id"]: n for n in collect()}
    assert "frontier" in nodes["gh:%s#13" % REPO]["badges"]        # open + 无 assignee
    assert "frontier" not in nodes["gh:%s#22" % REPO]["badges"]    # 有 assignee
    assert "claimed:lizzydeng" in nodes["gh:%s#22" % REPO]["badges"]
    assert "frontier" not in nodes["gh:%s#25" % REPO]["badges"]    # 已关


def test_blocked_issue_is_not_frontier():
    ctx = FakeCtx(deps={13: 1})
    nodes = {n["id"]: n for n in collect(ctx=ctx, fetch_dependencies=True)}
    assert "blocked" in nodes["gh:%s#13" % REPO]["badges"]
    assert "frontier" not in nodes["gh:%s#13" % REPO]["badges"]


def test_dependencies_not_fetched_by_default_keeps_call_count_low():
    ctx = FakeCtx()
    collect(ctx=ctx)
    assert not any("/issues/" in " ".join(a) and "/sub_issues" not in " ".join(a)
                   for a in ctx.calls)


def test_dependency_fetch_only_targets_open_children_of_groups():
    ctx = FakeCtx()
    collect(ctx=ctx, fetch_dependencies=True)
    probed = [a for a in ctx.calls
              if "/issues/" in " ".join(a) and "/sub_issues" not in " ".join(a)]
    numbers = {int(" ".join(a).rsplit("/issues/", 1)[1]) for a in probed}
    assert numbers == {13}   # 22/16 不是 map 的子票；25 已关；99 无 goal


def test_issue_without_goal_or_parent_is_kept_not_dropped():
    nodes = {n["id"]: n for n in collect()}
    assert "gh:%s#99" % REPO in nodes
    assert nodes["gh:%s#99" % REPO]["parent"] is None


def test_orphan_wayfinder_ticket_kept_with_no_parent():
    """#16 有 wayfinder:task 但不是任何 map 的子票 —— 必须留在图里。"""
    nodes = {n["id"]: n for n in collect()}
    assert nodes["gh:%s#16" % REPO]["parent"] is None


def test_milestone_goal_source():
    class MilestoneCtx(FakeCtx):
        def gh_json(self, args):
            if "issue" in args and "list" in args:
                return [{"number": 1, "title": "T", "state": "OPEN", "url": "u",
                         "body": "", "labels": [], "assignees": [],
                         "milestone": {"title": "Q3 变现"}}]
            return super().gh_json(args)

    nodes = {n["id"]: n for n in collect(ctx=MilestoneCtx(), goal_from="milestone",
                                        group_from=None)}
    assert nodes["goal:Q3 变现"]["kind"] == model.KIND_GOAL
    assert nodes["gh:%s#1" % REPO]["parent"] == "goal:Q3 变现"


def test_unknown_goal_from_form_raises():
    try:
        collect(goal_from="magic:xyz")
    except ValueError as exc:
        assert "magic" in str(exc)
    else:
        raise AssertionError("expected ValueError")
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python3 -m pytest tests/roadmap_radar/test_github_issues.py -v`
Expected: FAIL — `NotImplementedError: implemented in Task 5`

- [ ] **Step 4: 实现 `sources/github_issues.py`**

Replace `skills/roadmap-radar/scripts/sources/github_issues.py`:

```python
"""通用 GitHub source。任何仓库都能用，不含任何 wayfinder/forge 知识。

goal 从 label 正则或 milestone 来；层级从 GitHub sub-issues 来；
frontier = open + 无 assignee + 无 open blocker。
"""

import re

import model

ISSUE_FIELDS = "number,title,state,labels,assignees,url,body,milestone"
DEFAULT_LIMIT = 300


def collect(config, ctx):
    repo = config["repo"]
    limit = config.get("limit", DEFAULT_LIMIT)
    goal_from = config.get("goal_from") or "label:^obj-\\d+$"
    group_from = config.get("group_from")
    fetch_deps = bool(config.get("fetch_dependencies", False))

    issues = ctx.gh_json([
        "issue", "list", "--repo", repo, "--state", "all",
        "--limit", str(limit), "--json", ISSUE_FIELDS,
    ])

    nid = lambda number: "gh:%s#%s" % (repo, number)  # noqa: E731
    group_re = re.compile(group_from.partition(":")[2]) if group_from else None

    goals = {}
    nodes = []
    by_number = {}

    for issue in issues:
        raw_goals = _goal_raws(issue, goal_from)
        for raw in raw_goals:
            if raw not in goals:
                goals[raw] = model.node(
                    id="goal:%s" % raw, kind=model.KIND_GOAL, title=raw,
                    meta={"source": "github-issues", "repo": repo})
        is_group = bool(group_re) and any(
            group_re.search(label["name"]) for label in issue.get("labels", []))
        closed = str(issue.get("state", "")).lower() == "closed"
        assignees = [a["login"] for a in issue.get("assignees") or []]

        node = model.node(
            id=nid(issue["number"]),
            kind=model.KIND_GROUP if is_group else model.KIND_LEAF,
            title=issue["title"],
            url=issue.get("url"),
            parent=("goal:%s" % raw_goals[0]) if raw_goals else None,
            progress=None if is_group else (1.0 if closed else 0.0),
            state=str(issue.get("state", "")).lower(),
            badges=["claimed:%s" % a for a in assignees],
            meta={"source": "github-issues", "repo": repo,
                  "number": issue["number"], "body": issue.get("body") or "",
                  "labels": [label["name"] for label in issue.get("labels", [])]},
        )
        nodes.append(node)
        by_number[issue["number"]] = node

    # 层级：GitHub sub-issues。只对 group 节点问，成本 = group 数量。
    child_numbers = set()
    for node in nodes:
        if node["kind"] != model.KIND_GROUP:
            continue
        for child in ctx.gh_json([
            "api", "repos/%s/issues/%s/sub_issues" % (repo, node["meta"]["number"])
        ]) or []:
            target = by_number.get(child["number"])
            if target is None:
                continue
            target["parent"] = node["id"]
            child_numbers.add(child["number"])

    # blocker：只查 group 名下的 open 子票，避免 N 次 API
    blocked = set()
    if fetch_deps:
        for number in sorted(child_numbers):
            node = by_number[number]
            if node["state"] != "open":
                continue
            detail = ctx.gh_json(["api", "repos/%s/issues/%s" % (repo, number)]) or {}
            summary = detail.get("issue_dependencies_summary") or {}
            if summary.get("blocked_by", 0) > 0:
                blocked.add(number)
                node["badges"].append("blocked")

    for number, node in by_number.items():
        if (node["kind"] == model.KIND_LEAF and node["state"] == "open"
                and not any(b.startswith("claimed:") for b in node["badges"])
                and number not in blocked):
            node["badges"].append("frontier")

    return list(goals.values()) + nodes


def _goal_raws(issue, goal_from):
    """把 goal_from 配置翻译成这张 issue 归属的 goal 原始标识列表。"""
    form, _, arg = goal_from.partition(":")
    if form == "label":
        pattern = re.compile(arg)
        return [label["name"] for label in issue.get("labels", [])
                if pattern.search(label["name"])]
    if form == "milestone":
        milestone = issue.get("milestone") or {}
        title = milestone.get("title")
        return [title] if title else []
    raise ValueError(
        "unknown goal_from form %r; use 'label:<regex>' or 'milestone'" % form)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 -m pytest tests/roadmap_radar/test_github_issues.py -v`
Expected: PASS，12 passed

- [ ] **Step 6: 提交**

```bash
git add skills/roadmap-radar/scripts/sources/github_issues.py \
        tests/roadmap_radar/test_github_issues.py \
        tests/roadmap_radar/fixtures/gh_issues.json \
        tests/roadmap_radar/fixtures/gh_sub_issues_12.json
git commit -m "feat(roadmap-radar): 通用 github-issues source

label/milestone 当 goal、sub-issue 当层级、frontier=open+无主+无blocker。
gh 调用走 ctx 注入,测试零网络。依赖查询只打 group 子票,不做 N 次 API。"
```

---

### Task 6: `wayfinder` preset + map 正文分节

`github-issues` 的薄包装：固定 map label、开依赖查询、额外把 map 正文的 `## Decisions so far` / `## Not yet specified` / `## Out of scope` 解析进 meta，让页面能显示决策历史和迷雾。

**Files:**
- Modify: `skills/roadmap-radar/scripts/sources/wayfinder.py`（替换占位）
- Test: `tests/roadmap_radar/test_wayfinder.py`

- [ ] **Step 1: 写失败测试**

Create `tests/roadmap_radar/test_wayfinder.py`:

```python
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2] / "skills" / "roadmap-radar" / "scripts"
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(BASE))

import model  # noqa: E402
from sources import wayfinder  # noqa: E402
from test_github_issues import FakeCtx  # noqa: E402

REPO = "hyoteam/aifriends"


def collect(ctx=None, **cfg):
    cfg.setdefault("repo", REPO)
    return wayfinder.collect(dict(cfg, type="wayfinder"), ctx or FakeCtx())


def test_preset_finds_maps_without_group_from_config():
    nodes = {n["id"]: n for n in collect()}
    assert nodes["gh:%s#12" % REPO]["kind"] == model.KIND_GROUP
    assert nodes["gh:%s#14" % REPO]["kind"] == model.KIND_GROUP


def test_preset_turns_dependencies_on():
    ctx = FakeCtx(deps={13: 1})
    nodes = {n["id"]: n for n in collect(ctx=ctx)}
    assert "blocked" in nodes["gh:%s#13" % REPO]["badges"]


def test_map_body_sections_land_in_meta():
    nodes = {n["id"]: n for n in collect()}
    meta = nodes["gh:%s#12" % REPO]["meta"]
    assert meta["decisions"] == ["拆两轨 5 ticket,零丢弃", "抗漂移字段手填为准"]
    assert meta["fog"] == ["预算感知分层的分档阈值"]
    assert meta["out_of_scope"] == ["换记忆算法(北极星弱于我们)"]


def test_map_without_those_sections_gets_empty_lists_not_missing_keys():
    nodes = {n["id"]: n for n in collect()}
    meta = nodes["gh:%s#14" % REPO]["meta"]
    assert meta["decisions"] == []
    assert meta["fog"] == []


def test_leaf_nodes_are_not_section_parsed():
    nodes = {n["id"]: n for n in collect()}
    assert "decisions" not in nodes["gh:%s#13" % REPO]["meta"]


def test_config_can_override_preset():
    ctx = FakeCtx()
    collect(ctx=ctx, fetch_dependencies=False)
    probed = [a for a in ctx.calls
              if "/issues/" in " ".join(a) and "/sub_issues" not in " ".join(a)]
    assert probed == []


def test_section_parser_handles_bullets_with_links_and_bold():
    body = ("## Decisions so far\n\n"
            "- **D1**：挂 obj-3 变现（[#419](http://x) 已关）\n"
            "- 北极星集中在 F3 回收\n\n"
            "## Not yet specified\n\n- 准入门三条\n")
    parsed = wayfinder.parse_sections(body)
    assert parsed["decisions"][0].startswith("**D1**：挂 obj-3 变现")
    assert parsed["decisions"][1] == "北极星集中在 F3 回收"
    assert parsed["fog"] == ["准入门三条"]


def test_registry_exposes_wayfinder():
    from sources import REGISTRY
    assert "wayfinder" in REGISTRY
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/roadmap_radar/test_wayfinder.py -v`
Expected: FAIL — `NotImplementedError: implemented in Task 6`

- [ ] **Step 3: 实现 `sources/wayfinder.py`**

Replace `skills/roadmap-radar/scripts/sources/wayfinder.py`:

```python
"""wayfinder preset —— github-issues 加上 map 约定的那点知识。

约定来自 /wayfinder skill：map = label wayfinder:map，ticket = sub-issue，
map 正文有 Decisions so far / Not yet specified / Out of scope 三节。
"""

import re

import model
from . import github_issues

PRESET = {
    "group_from": "label:^wayfinder:map$",
    "goal_from": "label:^obj-\\d+$",
    "fetch_dependencies": True,
}

SECTION_KEYS = {
    "decisions so far": "decisions",
    "not yet specified": "fog",
    "out of scope": "out_of_scope",
}

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^[-*]\s+(.+?)\s*$")


def collect(config, ctx):
    merged = dict(PRESET)
    merged.update({k: v for k, v in config.items() if k != "type"})
    merged["type"] = "github-issues"
    nodes = github_issues.collect(merged, ctx)
    for node in nodes:
        if node["kind"] == model.KIND_GROUP:
            node["meta"].update(parse_sections(node["meta"].get("body", "")))
    return nodes


def parse_sections(body):
    """把 map 正文的三节抽成列表。缺的节返回空列表，不是缺 key。"""
    out = {key: [] for key in SECTION_KEYS.values()}
    current = None
    for line in (body or "").splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            current = SECTION_KEYS.get(heading.group(1).strip().lower())
            continue
        if current is None:
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            out[current].append(bullet.group(1))
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/roadmap_radar/test_wayfinder.py -v`
Expected: PASS，8 passed

- [ ] **Step 5: 提交**

```bash
git add skills/roadmap-radar/scripts/sources/wayfinder.py \
        tests/roadmap_radar/test_wayfinder.py
git commit -m "feat(roadmap-radar): wayfinder preset + map 正文三节解析

Decisions so far / Not yet specified / Out of scope 进 meta,
页面因此能显示决策历史和迷雾。缺节给空列表不缺 key。"
```

---

### Task 7: render.py — 自包含 HTML

原生 `<details>` 做展开，零 JS。内联 CSS，无 CDN。

**Files:**
- Create: `skills/roadmap-radar/scripts/render.py`
- Create: `tests/roadmap_radar/fixtures/graph.json`
- Test: `tests/roadmap_radar/test_render.py`

- [ ] **Step 1: 落渲染 fixture**

Create `tests/roadmap_radar/fixtures/graph.json`:

```json
{
  "nodes": [
    {"id": "forge:north-star", "kind": "goal", "title": "日均使用 30 分钟的 AI 情感陪伴平台",
     "url": null, "parent": null, "progress": 0.46, "state": null,
     "badges": [], "meta": {"kind": "north-star"}},
    {"id": "goal:obj-3", "kind": "goal", "title": "变现能力", "url": null,
     "parent": "forge:north-star", "progress": 0.12, "state": null, "badges": [],
     "meta": {"target_metric": "免费→付费转化率 >= 8%"}},
    {"id": "goal:obj-7", "kind": "goal", "title": "工程健康", "url": null,
     "parent": "forge:north-star", "progress": 1.0, "state": null, "badges": [],
     "meta": {"target_metric": "无 >300 LOC 巨石函数"}},
    {"id": "gh:hyoteam/aifriends#8", "kind": "group", "title": "约会探针《周末在家》",
     "url": "https://github.com/hyoteam/aifriends/issues/8", "parent": "goal:obj-3",
     "progress": null, "state": "open", "badges": ["empty", "no-count"],
     "meta": {"decisions": ["挂 obj-3 变现"], "fog": ["准入门三条未出结论"],
              "out_of_scope": []}},
    {"id": "gh:hyoteam/aifriends#14", "kind": "group", "title": "看广告换额度",
     "url": "https://github.com/hyoteam/aifriends/issues/14", "parent": "goal:obj-3",
     "progress": 0.5, "state": "open", "badges": [],
     "meta": {"decisions": [], "fog": [], "out_of_scope": []}},
    {"id": "gh:hyoteam/aifriends#22", "kind": "leaf", "title": "ads SSV 服务端验证",
     "url": "https://github.com/hyoteam/aifriends/issues/22",
     "parent": "gh:hyoteam/aifriends#14", "progress": 0.0, "state": "open",
     "badges": ["frontier"], "meta": {}},
    {"id": "gh:hyoteam/aifriends#25", "kind": "leaf", "title": "C 端接入",
     "url": "https://github.com/hyoteam/aifriends/issues/25",
     "parent": "gh:hyoteam/aifriends#14", "progress": 1.0, "state": "closed",
     "badges": [], "meta": {}},
    {"id": "forge:weekend-date-foundation", "kind": "leaf", "title": "Weekend Date Foundation",
     "url": null, "parent": "goal:obj-3", "progress": 0.0, "state": "draft",
     "badges": [], "meta": {}},
    {"id": "forge:chat-route-service-extraction", "kind": "leaf",
     "title": "Chat Route Service Extraction", "url": null, "parent": "goal:obj-7",
     "progress": 1.0, "state": "concluded", "badges": [], "meta": {}},
    {"id": "forge:ghost-one", "kind": "leaf", "title": "ghost-one", "url": null,
     "parent": null, "progress": null, "state": null,
     "badges": ["orphan", "no-count"],
     "meta": {"reason": "features/ 有目录但不在 roadmap 里"}},
    {"id": "gh:hyoteam/aifriends#16", "kind": "leaf",
     "title": "embedding 模型独立配 provider",
     "url": "https://github.com/hyoteam/aifriends/issues/16", "parent": null,
     "progress": 0.0, "state": "open", "badges": ["orphan", "frontier"], "meta": {}}
  ],
  "doctor": {
    "dangling_parents": [{"id": "gh:hyoteam/aifriends#16",
                          "title": "embedding 模型独立配 provider",
                          "missing_parent": "gh:hyoteam/aifriends#404"}],
    "orphans": [{"id": "forge:ghost-one", "title": "ghost-one"},
                {"id": "gh:hyoteam/aifriends#16", "title": "embedding"}],
    "empty_groups": [{"id": "gh:hyoteam/aifriends#8", "title": "约会探针《周末在家》"}],
    "unmapped_status": []
  }
}
```

- [ ] **Step 2: 写失败测试**

Create `tests/roadmap_radar/test_render.py`:

```python
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
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python3 -m pytest tests/roadmap_radar/test_render.py -v`
Expected: FAIL — `No module named 'render'`

- [ ] **Step 4: 写 `render.py`**

Create `skills/roadmap-radar/scripts/render.py`:

```python
"""Node 树 -> 单文件 HTML。域无关：只认 Node 的字段，不认业务。

展开靠原生 <details>，所以零 JS。CSS 内联，无 CDN，双击即开。
"""

import html as html_mod

import model

CSS = """
:root { color-scheme: light dark; --fg:#111; --dim:#666; --line:#ddd;
        --bar:#3b82f6; --ok:#16a34a; --warn:#dc2626; --card:#fff; --bg:#fafafa; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e7e7e7; --dim:#999; --line:#333; --card:#1a1a1a; --bg:#111; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
       font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",
       "PingFang SC","Hiragino Sans GB",sans-serif; }
header { border-bottom:1px solid var(--line); padding-bottom:14px; margin-bottom:20px; }
header .ns { font-size:19px; font-weight:600; }
header .meta { color:var(--dim); font-size:12px; margin-top:4px; }
.cards { display:grid; gap:12px; grid-template-columns:repeat(auto-fill,minmax(268px,1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:12px 14px; }
.card h2 { font-size:15px; margin:0 0 2px; }
.card .metric { color:var(--dim); font-size:11.5px; margin-bottom:9px;
                overflow-wrap:anywhere; }
.pct { font-variant-numeric:tabular-nums; font-weight:600; float:right; }
.bar { height:6px; background:var(--line); border-radius:3px; overflow:hidden;
       margin-bottom:10px; }
.bar > i { display:block; height:100%; background:var(--bar); }
.bar.full > i { background:var(--ok); }
details { margin:5px 0; }
summary { cursor:pointer; }
ul { list-style:none; margin:5px 0 5px 14px; padding:0;
     border-left:1px solid var(--line); }
li { padding:2px 0 2px 10px; }
a { color:inherit; }
.tag { display:inline-block; font-size:10.5px; padding:0 5px; border-radius:9px;
       border:1px solid var(--line); color:var(--dim); margin-left:5px;
       vertical-align:1px; }
.tag.frontier { border-color:var(--bar); color:var(--bar); }
.tag.warn { border-color:var(--warn); color:var(--warn); }
.done { color:var(--dim); text-decoration:line-through; }
.sections { color:var(--dim); font-size:12px; margin:3px 0 3px 14px; }
#unassigned { margin-top:26px; border:1px solid var(--warn); border-radius:10px;
              padding:12px 14px; }
#unassigned h2 { color:var(--warn); font-size:15px; margin:0 0 8px; }
table.dangling { border-collapse:collapse; font-size:12px; }
table.dangling td { padding:1px 10px 1px 0; }
"""


def esc(value):
    return html_mod.escape(str(value if value is not None else ""), quote=True)


def pct(value):
    return "—" if value is None else "%d%%" % round(value * 100)


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

    parts.append('<div class="cards">')
    for goal in goal_cards:
        parts.append(_goal_card(goal, nodes))
    parts.append("</div>")

    parts.append(_unassigned(nodes, report, by_id))
    parts.append("</body></html>")
    return "\n".join(parts)


def _goal_card(goal, nodes):
    value = goal["progress"]
    out = ['<div class="card">',
           '<h2>%s <span class="pct">%s</span></h2>' % (esc(goal["title"]), pct(value)),
           '<div class="bar%s"><i style="width:%d%%"></i></div>'
           % (" full" if (value or 0) >= 1.0 else "", round((value or 0) * 100))]
    metric = (goal["meta"] or {}).get("target_metric")
    if metric:
        out.append('<div class="metric">%s</div>' % esc(metric))
    for child in model.children_of(nodes, goal["id"]):
        out.append(_group_or_leaf(child, nodes))
    out.append("</div>")
    return "".join(out)


def _group_or_leaf(node, nodes):
    if node["kind"] == model.KIND_LEAF:
        return "<div>%s</div>" % _leaf_line(node)
    done, total = model.leaf_counts(nodes, node["id"])
    tags = ""
    if model.BADGE_EMPTY in node["badges"]:
        tags = '<span class="tag warn">空地图</span>'
    out = ["<details open><summary>%s <b>%d/%d</b>%s %s</summary>"
           % (_link(node), done, total, tags, pct(node["progress"]))]
    meta = node["meta"] or {}
    for key, label in (("decisions", "决策"), ("fog", "迷雾")):
        for item in meta.get(key) or []:
            out.append('<div class="sections">%s · %s</div>' % (label, esc(item)))
    children = model.children_of(nodes, node["id"])
    if children:
        out.append("<ul>")
        for child in children:
            out.append("<li>%s</li>" % _leaf_line(child))
        out.append("</ul>")
    out.append("</details>")
    return "".join(out)


def _leaf_line(node):
    done = (node["progress"] or 0) >= 1.0
    body = '<span class="%s">%s</span>' % ("done" if done else "", _link(node))
    for badge in node["badges"]:
        if badge == "frontier":
            body += '<span class="tag frontier">frontier</span>'
        elif badge in ("blocked", "orphan", "unmapped-status", "cycle"):
            body += '<span class="tag warn">%s</span>' % esc(badge)
        elif badge.startswith("claimed:"):
            body += '<span class="tag">%s</span>' % esc(badge)
    if node["state"] and node["kind"] == model.KIND_LEAF:
        body += '<span class="tag">%s</span>' % esc(node["state"])
    return body


def _link(node):
    title = esc(node["title"])
    return '<a href="%s">%s</a>' % (esc(node["url"]), title) if node["url"] else title


def _unassigned(nodes, report, by_id):
    orphans = [n for n in nodes if "orphan" in n["badges"]]
    dangling = report.get("dangling_parents") or []
    empty = report.get("empty_groups") or []
    unmapped = report.get("unmapped_status") or []
    if not (orphans or dangling or empty or unmapped):
        return ""
    out = ['<div id="unassigned"><h2>⚠️ 未归位 / 需要体检</h2>']
    if orphans:
        out.append("<div><b>没有归属的节点（%d）</b><ul>" % len(orphans))
        for node in orphans:
            reason = (node["meta"] or {}).get("reason") or ""
            out.append("<li>%s <span class=\"tag\">%s</span></li>"
                       % (_link(node), esc(reason)))
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
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 -m pytest tests/roadmap_radar/test_render.py -v`
Expected: PASS，11 passed

- [ ] **Step 6: 提交**

```bash
git add skills/roadmap-radar/scripts/render.py tests/roadmap_radar/test_render.py \
        tests/roadmap_radar/fixtures/graph.json
git commit -m "feat(roadmap-radar): 自包含 HTML 渲染

原生 details 展开零 JS,CSS 内联无 CDN,亮暗双主题。
空地图显示「空」不显示 0%,未归位区把断链证据(缺哪个父 id)打出来。"
```

---

### Task 8: radar.py CLI + SKILL.md + 真实数据烟测

**Files:**
- Create: `skills/roadmap-radar/scripts/radar.py`
- Create: `skills/roadmap-radar/SKILL.md`
- Create: `tests/roadmap_radar/test_cli.py`
- Modify: `CLAUDE.md`（skills 列表加一行）

- [ ] **Step 1: 写失败测试**

Create `tests/roadmap_radar/test_cli.py`:

```python
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "skills" / "roadmap-radar" / "scripts" / "radar.py"
FIX = Path(__file__).resolve().parent / "fixtures"


def run(*args, cwd=None):
    return subprocess.run([sys.executable, str(RADAR), *args],
                          capture_output=True, text=True, cwd=cwd)


def test_from_graph_renders_without_touching_network(tmp_path):
    out = tmp_path / "radar.html"
    result = run("--from", str(FIX / "graph.json"), "--out", str(out), "--no-open")
    assert result.returncode == 0, result.stderr
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "变现能力" in html


def test_config_discovery_reads_dot_radar_json(tmp_path):
    (tmp_path / "project-roadmap").mkdir()
    (tmp_path / "project-roadmap" / "roadmap.json").write_text(json.dumps({
        "north_star": "NS",
        "objectives": [{"id": "obj-1", "name": "G", "target_metric": "M"}],
        "features": [{"slug": "f", "name": "F", "objective_id": "obj-1",
                      "status": "concluded"}],
    }), encoding="utf-8")
    (tmp_path / ".radar.json").write_text(json.dumps({
        "sources": [{"type": "forge", "path": "project-roadmap/roadmap.json"}]
    }), encoding="utf-8")

    graph_out = tmp_path / "graph.json"
    result = run("--graph", str(graph_out), "--no-open", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    graph = json.loads(graph_out.read_text(encoding="utf-8"))
    by_id = {n["id"]: n for n in graph["nodes"]}
    assert by_id["goal:obj-1"]["progress"] == 1.0


def test_doctor_prints_report_and_writes_nothing(tmp_path):
    (tmp_path / "project-roadmap").mkdir()
    (tmp_path / "project-roadmap" / "roadmap.json").write_text(json.dumps({
        "north_star": "NS",
        "objectives": [{"id": "obj-1", "name": "G", "target_metric": "M"}],
        "features": [{"slug": "f", "name": "F", "objective": "obj-404",
                      "status": "weird-word"}],
    }), encoding="utf-8")
    (tmp_path / ".radar.json").write_text(json.dumps({
        "sources": [{"type": "forge", "path": "project-roadmap/roadmap.json"}]
    }), encoding="utf-8")

    result = run("--doctor", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "obj-404" in result.stdout
    assert "weird-word" in result.stdout
    assert not (tmp_path / "radar.html").exists()


def test_missing_config_exits_nonzero_with_actionable_message(tmp_path):
    result = run("--no-open", cwd=tmp_path)
    assert result.returncode != 0
    assert ".radar.json" in (result.stderr + result.stdout)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/roadmap_radar/test_cli.py -v`
Expected: FAIL — `can't open file .../radar.py`

- [ ] **Step 3: 写 `radar.py`**

Create `skills/roadmap-radar/scripts/radar.py`:

```python
#!/usr/bin/env python3
"""roadmap-radar CLI —— goal 拓扑与进度可视化。

    radar.py                     # collect + render + 开浏览器
    radar.py --out radar.html
    radar.py --graph graph.json  # 只 collect
    radar.py --from graph.json   # 只 render（离线迭代 UI，不打 API）
    radar.py --doctor            # 只报告断链，零写入
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import collect as collect_mod  # noqa: E402
import render as render_mod  # noqa: E402
from sources import REGISTRY  # noqa: E402

CONFIG_NAME = ".radar.json"


class Ctx:
    """给 source 用的 IO 出口。测试注入替身，生产打 gh。"""

    def __init__(self, root):
        self.root = root

    def resolve(self, rel):
        return rel if os.path.isabs(rel) else os.path.join(self.root, rel)

    def gh_json(self, args):
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("gh %s failed: %s" % (" ".join(args), proc.stderr.strip()))
        text = proc.stdout.strip()
        return json.loads(text) if text else []


def load_config(root, explicit):
    path = explicit or os.path.join(root, CONFIG_NAME)
    if not os.path.isfile(path):
        raise SystemExit(
            "找不到 %s。在项目根建一个，例如：\n"
            '{"sources": [\n'
            '  {"type": "forge", "path": "project-roadmap/roadmap.json"},\n'
            '  {"type": "wayfinder", "repo": "owner/name"}\n'
            "]}" % (explicit or CONFIG_NAME)
        )
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def print_doctor(report):
    order = (("dangling_parents", "父节点找不到（断链）"),
             ("orphans", "没有归属"),
             ("empty_groups", "空的 goal / 地图"),
             ("unmapped_status", "状态词没映射"))
    total = 0
    for key, label in order:
        rows = report.get(key) or []
        total += len(rows)
        print("\n%s（%d）" % (label, len(rows)))
        for row in rows:
            title = row.get("title") or row.get("id")
            extra = row.get("missing_parent") or row.get("state") or ""
            print("  - %s%s" % (title, (" → %s" % extra) if extra else ""))
    print("\n共 %d 项。只报告不修 —— 修法是项目专属的。" % total)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="radar")
    parser.add_argument("--config")
    parser.add_argument("--root", default=os.getcwd())
    parser.add_argument("--out", default="radar.html")
    parser.add_argument("--graph")
    parser.add_argument("--from", dest="from_graph")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)

    if args.from_graph:
        with open(args.from_graph, encoding="utf-8") as handle:
            graph = json.load(handle)
    else:
        config = load_config(args.root, args.config)
        graph = collect_mod.build(config, Ctx(args.root), REGISTRY)

    if args.doctor:
        print_doctor(graph.get("doctor") or {})
        return 0

    if args.graph:
        with open(args.graph, "w", encoding="utf-8") as handle:
            json.dump(graph, handle, ensure_ascii=False, indent=2)
        print("graph → %s" % args.graph)
        if not args.out:
            return 0

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    html = render_mod.render(graph, generated_at=stamp)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(html)
    print("radar → %s" % os.path.abspath(args.out))
    if not args.no_open:
        webbrowser.open("file://" + os.path.abspath(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/roadmap_radar/test_cli.py -v`
Expected: PASS，4 passed

- [ ] **Step 5: 跑全套测试**

Run: `python3 -m pytest tests/roadmap_radar/ -v`
Expected: PASS，共约 67 项全绿

- [ ] **Step 6: 写 SKILL.md**

Create `skills/roadmap-radar/SKILL.md`:

```markdown
---
name: roadmap-radar
description: Use when the user wants to see the topology and progress of a project's goals — how far each objective has gotten across its forge features and wayfinder issue maps, which ticket is takeable right now (frontier), and what is not filed under any goal. Triggers include "看 roadmap 进度", "goal 整体进度", "issue 拓扑", "issue 地图", "哪张票能动", "wayfinder 进度", "roadmap radar", "objective progress", "what can I work on next".
---

# roadmap-radar

把 forge roadmap 和 wayfinder GitHub issue 合成一张自包含 HTML，回答三个问题：

1. 每个 goal 整体走到哪了（跨 feature + issue 的 rollup）
2. 现在能动的是哪张票（frontier）
3. 什么东西没归位（孤儿 feature / 无 goal 的 issue / 迁仓断链）

## 用法

```bash
SCRIPT=/Users/apple/works/claude-skills/skills/roadmap-radar/scripts/radar.py

python3 $SCRIPT                        # 生成 radar.html 并打开
python3 $SCRIPT --graph graph.json     # 只落中间数据
python3 $SCRIPT --from graph.json      # 只渲染（离线改 UI，不打 API）
python3 $SCRIPT --doctor               # 只体检，零写入
```

在项目根建 `.radar.json` 声明数据源，**不写代码**：

```jsonc
{ "sources": [
  { "type": "forge",     "path": "project-roadmap/roadmap.json" },
  { "type": "wayfinder", "repo": "owner/name" }
]}
```

## 三个内置 source

| type | 用途 | 关键配置 |
|---|---|---|
| `forge` | `/forge` 的 roadmap（json 优先、md 兜底） | `path`、`goal_field`、`title_field`、`status_progress` |
| `wayfinder` | `/wayfinder` 的 map + 子票 | `repo`、`goal_from`（默认 `label:^obj-\d+$`） |
| `github-issues` | 任何 GitHub 仓，零 forge 依赖 | `repo`、`goal_from`（`label:<regex>` 或 `milestone`）、`group_from` |

## 关键行为

- **goal id 跨 source 统一成 `goal:<raw>`** —— forge 的 `obj-3` 和 GitHub label `obj-3` 因此合成同一个节点，这是 goal 进度能跨两个数据源的机制。
- **进度每层等权** —— 一张 6 票的 map 和一个 feature 在 goal 眼里各算一票。
- **空地图不显示 0%**，显示「空」并且不把父级分母拉大。
- **未映射的状态词不当 0%**，返回未知并在体检里报出来。
- **永不丢节点** —— parent 找不到就断开、打 orphan、把原 parent 存进证据，全部出现在「未归位」区。

## 前置

- `gh` CLI 已登录（只有 GitHub source 需要）
- Python 3，零第三方依赖
```

- [ ] **Step 7: CLAUDE.md 加一行**

在 `CLAUDE.md` 的 Skills 列表末尾追加：

```markdown
- `/roadmap-radar` — goal 拓扑与进度可视化：forge roadmap + wayfinder issue 合成一张自包含 HTML（含 frontier 和未归位体检）
```

- [ ] **Step 8: 真实数据烟测（不能只靠 fixture）**

```bash
cd /Users/apple/works/viber-code/aifriends
cat > .radar.json <<'EOF'
{ "sources": [
  { "type": "forge", "path": "project-roadmap/roadmap.json" },
  { "type": "wayfinder", "repo": "hyoteam/aifriends" }
]}
EOF
python3 /Users/apple/works/claude-skills/.claude/worktrees/roadmap-radar/skills/roadmap-radar/scripts/radar.py --doctor
```

Expected：体检报出 hyoteam 的断链和空地图 —— `约会探针` 空地图、`obj-6` 在 features 里被引用但 objectives 里没有、11 个未登记的 `features/` 目录。

```bash
python3 .../radar.py --out /tmp/radar-aifriends.html --no-open
python3 -c "print(open('/tmp/radar-aifriends.html').read().count('card'))"
```

Expected：HTML 生成成功，goal 卡片数 = roadmap.json 的 objectives 数（6）。

同样跑一遍 supersdk（验证字段探测在真数据上命中）：

```bash
cd /Users/apple/works/viber-code/supersdk
echo '{"sources":[{"type":"forge","path":"project-roadmap/roadmap.json"}]}' > .radar.json
python3 .../radar.py --out /tmp/radar-supersdk.html --no-open
```

Expected：两个 goal（用户体系 / 变现体系），三个 feature 各就位。

再跑 heartstory（验证 md 兜底在真数据上命中）：

```bash
cd /Users/apple/works/viber-code/heartstory
echo '{"sources":[{"type":"forge","path":"project-roadmap/roadmap.json"}]}' > .radar.json
python3 .../radar.py --out /tmp/radar-heartstory.html --no-open
```

Expected：json 不存在 → 自动退 `roadmap.md`，三个 goal 出得来。

**三个项目任一失败就回去修 source，不要改测试迁就。** 把失败输出贴出来。

- [ ] **Step 9: 提交**

```bash
git add skills/roadmap-radar/scripts/radar.py skills/roadmap-radar/SKILL.md \
        tests/roadmap_radar/test_cli.py CLAUDE.md
git commit -m "feat(roadmap-radar): CLI + SKILL.md

--graph/--from 把 collect 和 render 解耦(离线迭代 UI 不打 API),
--doctor 只报告零写入。真实数据烟测覆盖 aifriends/supersdk/heartstory
三种 roadmap 形态。"
```

---

### Task 9: 推 PR

- [ ] **Step 1: 全套测试 + 确认无残留**

```bash
cd /Users/apple/works/claude-skills/.claude/worktrees/roadmap-radar
python3 -m pytest tests/roadmap_radar/ -q
git status --short
```
Expected：全绿，工作区干净。

- [ ] **Step 2: 推分支开 draft PR**

```bash
git push -u origin feat/roadmap-radar
gh pr create --draft --base main \
  --title "feat(roadmap-radar): goal 拓扑与进度可视化 skill" \
  --body "$(cat <<'EOF'
## 问题

用 /wayfinder 管 issue、/forge 管 roadmap，但看不到一个 goal 派生出的全部工作走到哪了。
实测 aifriends：40 张 issue 里只有 1 张提到 obj-N，feature 零 issue 号，
hyoteam 迁仓丢光 sub-issue 父子边，全仓 0 条 blocking。**边不存在，不是渲染问题。**

## 方案

域无关核心（Node + rollup 纯函数 + render）+ source 注册表。
`rollup`/`render` 完全不知道 forge/GitHub 存在，只见 Node 树。

三个 source：`forge`（roadmap.json 优先、roadmap.md 兜底）、`wayfinder`、`github-issues`。
项目侧只加 `.radar.json`，零代码。

## 验证

- 单测覆盖 rollup（等权/空组/None 不当 0/环）、合并去重、三种 roadmap 形态、gh 录制回放、渲染断言
- 真实数据烟测三个项目：aifriends（json + wayfinder）、supersdk（字段名不同）、heartstory（只有 md）

## 不在范围

aifriends 的一次性补边（9 张 closed 存根 + 13 条 sub-issue 边 + 24 对编号映射）
是修数据不是看数据，另开，落 aifriends 仓。skill 只留通用 `--doctor` 报告。

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_013mKzZgeoG1RhytV9jQqWMS
EOF
)"
```

---

## Self-Review

**Spec 覆盖核对：**

| spec 要求 | 落在 |
|---|---|
| Node 模型 | Task 1 |
| rollup 等权 / 空组 no-count / leaf progress 由 source 给 | Task 1 |
| source 注册表禁 if-else | Task 3（`sources/__init__.py`） |
| `forge` json reader + 字段探测 | Task 3 |
| `forge` md reader（heartstory） | Task 3 实现 / Task 4 真数据验证 |
| features/ 孤儿目录进图 | Task 3 |
| `github-issues` label/milestone goal + sub-issue 层级 + frontier | Task 5 |
| `wayfinder` preset + 正文三节 | Task 6 |
| 合并去重 / 标 orphan / 永不丢节点 | Task 2 |
| doctor 四类检查 | Task 2 + Task 8 CLI |
| 自包含 HTML / 未归位一等公民 | Task 7 |
| CLI 四个模式 | Task 8 |
| 一次性补边移出 skill | 不实现，PR 正文写明 |

**偏离 spec 处（已在文首声明）：** `forge-json`+`forge-md` 合并为 `forge`；测试放仓库根。

**命名一致性核对：** `model.node/rollup/children_of/roots/copy_nodes/children_map/leaf_counts/BADGE_*`、`collect.merge/mark_orphans/doctor/build`、`render.render/esc/pct`、`sources.REGISTRY`、每个 source 的 `collect(config, ctx)`、`ctx.resolve/gh_json` —— 各任务用词一致。`wayfinder.parse_sections` 在 Task 6 测试和实现里同名。
