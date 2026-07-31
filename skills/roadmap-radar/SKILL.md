---
name: roadmap-radar
description: Use when the user wants to see the topology and progress of a project's goals — how far each objective has gotten across its forge features and wayfinder issue maps, which ticket is takeable right now (frontier), and what is not filed under any goal. Triggers include "看 roadmap 进度", "goal 整体进度", "issue 拓扑", "issue 地图", "哪张票能动", "wayfinder 进度", "roadmap radar", "objective progress", "what can I work on next".
---

# roadmap-radar

把 forge roadmap 和 wayfinder GitHub issue 合成一张自包含 HTML（每个 goal 一张
「圆点 + 连线」的 tidy tree 拓扑图，SVG 由 Python 直接算好，零 JS），回答三个问题：

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

# 不想在项目里放 .radar.json（试跑、或别人的仓库）：
python3 $SCRIPT --root /path/to/project --config /tmp/that.radar.json --out /tmp/r.html --no-open
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
- **层级 = GitHub sub-issues，一次分页 GraphQL 拉全仓的边** —— 子票挂在上游 issue 下（不只挂 map 下、任意深度）也能建出链；跨仓子票被过滤（拿 number 撞本仓会乱挂）。有子票的票不算 frontier。
- **节点标题/一句话简介/决策全文在 hover tooltip** —— 图面只放圆点、截断标题和 done/total 计数；一句话简介 = issue body 首个有效行 / forge objective 的 description。
- **进度每层等权** —— 一张 6 票的 map 和一个 feature 在 goal 眼里各算一票。
- **空地图不显示 0%**，显示「空」并且不把父级分母拉大。
- **未映射的状态词不当 0%**，返回未知并在体检里报出来。
- **永不丢节点** —— 挂不到 goal 下面的节点全部落在「未归位」区，**连整棵子树一起**。两类分开报：
  - *父节点已消失*：parent 指向不存在的 id（迁仓断链）→ 断开、打 `orphan`、把原 parent 存进证据
  - *没挂到任何 goal*：parent 从出生就是 None（issue 没打 goal label）→ 连它的子票一起显示
- **`--doctor` 只报告不修** —— 修法是项目专属的，报告是通用的。

## 前置

- `gh` CLI 已登录（只有 GitHub source 需要）
- Python 3，零第三方依赖
