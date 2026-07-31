---
name: roadmap-radar
description: Use when the user wants to see the topology and progress of a project's goals — how far each objective has gotten across its forge features and wayfinder issue maps, which ticket is takeable right now (frontier), and what is not filed under any goal. Triggers include "看 roadmap 进度", "goal 整体进度", "issue 拓扑", "issue 地图", "哪张票能动", "wayfinder 进度", "roadmap radar", "objective progress", "what can I work on next".
---

# roadmap-radar

把 GitHub issue 的**先后依赖关系**（native blocking）渲成一张自包含 HTML：
每个 map / 带子票的上游票一条「泳道」，泳道内按 longest-path 分层 ——
无前置的票在第 0 列，被 block 的票在其所有前置右边，箭头 = 先做左边才能做右边。
圆点 + 连线，SVG 由 Python 直接算好，零 JS。回答三个问题：

1. 一张 map 里的票谁先谁后、几条链在哪汇聚
2. 现在能动的是哪张票（frontier = 前置全关 + 没人认领，灰点蓝圈）
3. 什么在卡（红箭头 = open blocker）、什么没归位（散票 / 迁仓断链）

颜色：绿=已关、橘=正在做（有认领）、灰=未开始；泳道外的前置票画虚线幽灵点。

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

- **先后边 = GitHub native blocking，逐票拉 `/dependencies/blocked_by` 全列表**（含已关票 —— 链条历史不完整会看不懂一张票为什么能动）；正文里文字写的「依赖 #x」不解析（噪音）。跨仓 blocker 被过滤。
- **归属边 = GitHub sub-issues，一次分页 GraphQL 拉全仓** —— 只用来分泳道（谁属于哪张 map），不参与先后布局；子票挂上游 issue 下（任意深度）也认。
- **frontier = open + 无认领 + 自己没子票 + 前置全关**，和 DAG 语义合一。
- **泳道优先级 = map 票上的 `P0`–`P9` label**：泳道按优先级排序（P0 最前），泳道头显示色阶徽标（P0 红 / P1 橘 / P2 蓝 / 其余灰）；没打 label 的泳道排最后、无徽标。优先级是产品决策，radar 只读不造。
- **goal 已降级为标签**：不再是页面结构（forge 解耦）。issue 有 obj label 时 goal 只出现在泳道 chip 和页脚一行；forge source 仍可配（其 feature 会落散票区），但默认建议不配。
- **节点标题/一句话简介/决策全文在 hover tooltip** —— 图面只放圆点和截断标题；一句话简介 = issue body 首个有效行。
- **进度每层等权** —— 一张 6 票的 map 和一个 feature 在 goal 眼里各算一票。
- **空地图不显示 0%**，显示「空」并且不把父级分母拉大。
- **未映射的状态词不当 0%**，返回未知并在体检里报出来。
- **永不丢节点** —— 挂不到 goal 下面的节点全部落在「未归位」区，**连整棵子树一起**。两类分开报：
  - *父节点已消失*：parent 指向不存在的 id（迁仓断链）→ 断开、打 `orphan`、把原 parent 存进证据
  - *没挂到任何 goal*：parent 从出生就是 None（issue 没打 goal label）→ 连它的子票一起显示
- **`--doctor` 只报告不修** —— 修法是项目专属的，报告是通用的。

## 前置

- `gh` CLI 已登录（只有 GitHub source 需要）
- Python 3，零 pip 依赖
- **graphviz**（`brew install graphviz`）—— DAG 布局引擎，构建期跑 `dot -Tsvg`，页面本身仍是零 JS 静态 SVG
