# roadmap-radar — goal 拓扑与进度可视化

日期：2026-07-30
状态：设计已确认，待实现

## 问题

用户用 `/wayfinder` 管 issue、用 `/forge` 管 roadmap，但**看不到一个 goal 派生出的全部工作走到哪了**。

实测 aifriends（2026-07-30）证实这不是渲染问题，是**边不存在**：

| 断的边 | 实测 |
|---|---|
| objective → issue | 40 张 issue 里只有 1 张（#492）提到 `obj-N` |
| feature → PR/issue | feature 只有 `slug/status/日期`，零 issue 号 |
| roadmap.json(45) ↔ features/(56) | 11 个目录不在 roadmap.json |
| map → ticket（hyoteam） | 迁仓丢光，`sub_issues` 全空 |
| blocking | 全仓 0 条 |

同时，"通用"不是臆想 —— 同一个 `/forge` 在三个项目生出三种 schema：

| 项目 | 文件 | goal 字段 | 标题字段 |
|---|---|---|---|
| aifriends | `roadmap.json` | `objective_id` | `north_star` |
| supersdk | `roadmap.json` | `objective` | `project` |
| heartstory | **只有 `roadmap.md`** | — | — |

硬编 reader 在 supersdk 上 KeyError，在 heartstory 上找不到文件。

## Destination

一条命令生成一张**自包含 HTML**，回答三个问题：

1. 每个 goal 整体走到哪了（跨 forge feature + wayfinder map 的 rollup）
2. 现在能动的是哪张票（frontier）
3. 什么东西没归位（孤儿 feature / 无 goal 的 issue / 断链）

跨项目可用，接新项目只加一个 config 文件、不写代码。

## 架构

**这是 ETL + 渲染，不是 dashboard。** 核心域无关，source 走注册表。

```
┌─ 域无关核心（唯一"聪明"的部分，可单测）────────────┐
│  Node 模型                                          │
│  rollup(nodes) -> nodes    纯函数                    │
│  render(nodes) -> 自包含 HTML                        │
└──────────────────▲──────────────────────────────────┘
                   │ REGISTRY 分发，禁 if-else
   ┌───────────────┼──────────────┬──────────────┐
 github-issues  wayfinder     forge-json      forge-md
  (任何仓)       (preset)   (aifriends/       (heartstory)
                             supersdk)
```

`rollup` 与 `render` **完全不知道** forge / wayfinder / GitHub 存在，只见 Node 树。

### Node 模型

```python
Node = {
  "id":       str,          # 全局唯一，带 source 前缀
                            #   "obj:obj-3" / "gh:hyoteam/aifriends#12" / "forge:her-world-gift-wall"
  "kind":     "goal" | "group" | "leaf",
  "title":    str,
  "url":      str | None,   # 点击跳转（GitHub URL 或 file:// 路径）
  "parent":   str | None,   # 父 Node id
  "progress": float | None, # 0.0-1.0；leaf 必填，group/goal 留 None 由 rollup 填
  "state":    str | None,   # 原始状态字串，仅展示（"concluded" / "open" / "closed"）
  "badges":   [str],        # frontier / empty / orphan / blocked / claimed:<user> / no-count
  "meta":     {}            # source 特有字段，渲染器只做 key:value 展示
}
```

**`parent` 是单值不是列表** —— 有意为之。DAG 上的 rollup 会重复计数、语义歧义；树让 rollup 是一个定义良好的纯 fold。source 若真需要多父，就发多个 id 不同的重复 leaf。

### rollup（纯函数）

自底向上：

```
children = [c for c in nodes if c.parent == n.id and "no-count" not in c.badges]
if not children:  progress = None; badges += ["empty", "no-count"]   # rollup 自动打
else:             progress = mean(rollup(c) for c in children)       # 等权
```

**每层等权**。理由：票多的 group 不该淹没 feature —— 一张 6 票的 map 和一个 feature 在 objective 眼里各算一票。

**`no-count` 由 rollup 自动打在 0 子节点的 group/goal 上**，因此空地图既不显示假进度，也不把父级分母拉大。source 也可主动打 `no-count` 显式排除某节点。

**leaf 的 progress 由 source 必填**，核心不猜：
- `github-issues` / `wayfinder`：`closed → 1.0`，`open → 0.0`（二值，issue 没有中间态）
- `forge-json` / `forge-md`：走 `status_progress` 词表映射

**状态词表 → 数值的映射属于 source，不属于核心。** 这正是 supersdk 换个词表只改 config 的原因。

### source 契约

```python
# skills/roadmap-radar/scripts/sources/<name>.py
def collect(config: dict, ctx: Ctx) -> list[Node]: ...

# sources/__init__.py
REGISTRY = {"github-issues": ..., "wayfinder": ..., "forge-json": ..., "forge-md": ...}
```

注册表分发，无 if-else 链（遵循项目规矩 `no-ifelse`）。

### 四个内置 source

**1. `github-issues`** — 零 forge 依赖，任何 GitHub 仓开箱即用

```json
{ "type": "github-issues", "repo": "owner/name",
  "goal_from": "label:^obj-\\d+$",
  "group_from": "label:^wayfinder:map$",
  "leaf_state_done": ["closed"] }
```

`goal_from` 支持 `label:<regex>` 和 `milestone`。父子边优先用 GitHub sub-issues API，没有则按 goal label 直挂。
badges：`frontier`（open + 无 open blocker + 无 assignee）、`blocked`、`claimed:<user>`。

**2. `wayfinder`** — 上者的 preset

固定 `group_from = label:wayfinder:map` + sub-issues + native dependencies，额外解析 map 正文的 `## Decisions so far` / `## Not yet specified` 塞进 `meta`，让 UI 能显示决策历史与迷雾。

**3. `forge-json`**

```json
{ "type": "forge-json", "path": "project-roadmap/roadmap.json",
  "goal_field": "objective_id", "title_field": "north_star",
  "status_progress": {"draft":0,"planned":0.1,"in_progress":0.5,"measuring":0.85,"concluded":1.0} }
```

`goal_field` 缺省按序探测 `objective_id` → `objective`；`title_field` 探测 `north_star` → `project`。
**探测顺序写死在 source 里，config 可覆盖** —— supersdk 因此零配置也能跑。

**4. `forge-md`** — heartstory 那种只有 `roadmap.md` 的

解析 markdown 标题层级 + checkbox。宽松：解析失败产出 `orphan` 节点，不崩。

### 合并与"未归位"

`collect` 跑完所有 source → 合并 Node 列表 → 按 id 去重 → 任何 `parent` 指向不存在 id 的节点：`parent=None` + badge `orphan`。

**永不丢节点。** 孤儿全部进 UI 的"未归位"区，红色显示。这是一等公民，不是脚注 —— 悄悄丢节点的进度条会撒谎。

### 页面

```
北极星：日均使用 30 分钟的 AI 情感陪伴平台
─────────────────────────────────────────────
[obj-1 对话粘性 59%] [obj-2 情感真实感 62%] [obj-3 变现 12%]
[obj-5 引擎迁移 20%] [obj-6 Her World 83%] [obj-7 工程健康 100%✅]
─────────────────────────────────────────────
▼ obj-3 变现能力          转化率 ≥8% / 月付费留存 ≥70%
   ┌ forge features ────┬ wayfinder maps ─────────────┐
   │ weekend-date  draft│ 《周末在家》探针   0/0 ⚠️空图 │
   │ video-gen     draft│  └ frontier: 准入门 3 条未结 │
   │                    │ 看广告换额度      2/3 ███░  │
   │                    │  └ ▶ #22 ads SSV 服务端验证 │
   └────────────────────┴──────────────────────────────┘
─────────────────────────────────────────────
⚠️ 未归位  11 个 features/ 目录不在 roadmap.json · N 张 issue 无 obj label
```

技术：`python3` stdlib + `gh` CLI，零第三方依赖。HTML 内联 CSS/JS，无 CDN，双击即开。

### CLI

```
radar.py                     # collect + render + 开浏览器
radar.py --out radar.html
radar.py --graph graph.json  # 只 collect，落 graph.json
radar.py --from graph.json   # 只 render（离线迭代 UI，不打 API）
radar.py --doctor            # 只报告断链，零写入
```

`--doctor` 报告（**只报不修**，修法是项目专属的）：
- orphan 节点
- 空 group（0 子节点）
- `parent` 指向不存在的 id —— 这就是 aifriends 迁仓的病灶
- 同一 goal 下出现未映射的状态词

## 文件布局

```
skills/roadmap-radar/
  SKILL.md
  scripts/
    radar.py            # CLI 入口
    model.py            # Node + rollup（纯函数，域无关）
    render.py           # Node[] -> 自包含 HTML（域无关）
    collect.py          # 跑 source + 合并 + 标 orphan
    sources/
      __init__.py       # REGISTRY
      github_issues.py
      wayfinder.py
      forge_json.py
      forge_md.py
  tests/
    fixtures/
      aifriends-roadmap.json     # 真实抽样
      supersdk-roadmap.json      # 真实抽样（验证字段探测）
      graph.json                 # 渲染用固定输入
    test_rollup.py
    test_sources.py
    test_render.py
```

项目侧只加一个 `.radar.json`：

```jsonc
// aifriends/.radar.json
{ "sources": [
  { "type": "forge-json", "path": "project-roadmap/roadmap.json" },
  { "type": "wayfinder", "repo": "hyoteam/aifriends", "goal_from": "label:^obj-\\d+$" }
]}

// supersdk/.radar.json —— 字段探测自动命中，零覆盖
{ "sources": [{ "type": "forge-json", "path": "project-roadmap/roadmap.json" }] }
```

## 测试

| 测试 | 内容 |
|---|---|
| `test_rollup.py` | 纯函数：等权平均 / 空组 / `no-count` 排除 / 深层嵌套 |
| `test_sources.py` | 拿 aifriends + supersdk 两份**真** roadmap.json 当 fixture，断言字段探测都命中 |
| `test_render.py` | fixture graph.json → HTML，断言含 goal 标题、进度数字、未归位区 |

github source 不测网络，用录制的 gh json fixture。

## 明确不做（out of scope）

- **aifriends 的一次性数据修复** —— 9 张 closed 存根回填、13 条 sub-issue 边重建、24 对编号映射。这是修数据不是看数据，落 `aifriends/scripts/one-off/backfill_hyoteam_graph.py`，跑完即删。skill 里只留通用的 `--doctor` 报告断链。
- 常驻 server / 实时刷新（40 个节点用不着）
- 写回 GitHub —— skill 只读
- D3 力导向图 —— 0 条 blocking 边，画出来是 4 棵星形树，零信息量

## 附：一次性补边脚本的输入（需用户确认后才跑）

在 hyoteam/aifriends 上执行，每步先查后写、幂等，先 `--dry-run`：

1. 建 6 个 label `obj-1/2/3/5/6/7`（roadmap 无 obj-4）
2. 回填 9 张 closed 存根（标题照抄，正文只放 `原票 okteam99/aifriends#NNN` + 一行 gist，建完立即 close）：
   `509, 520`（map #9）· `534, 535, 536, 537, 594`（map #12）· `551, 554`（map #14）
3. 重建 13 条 sub-issue 父子边
4. 给 4 张 map 打 obj label —— **归属待确认**：

   | map | 推断归属 | 依据 |
   |---|---|---|
   | #8 约会探针《周末在家》 | obj-3 变现 | 正文 D1 明写"挂 obj-3" ✅ |
   | #14 看广告换额度 | obj-3 变现 | credits 直接变现 |
   | #12 polaris 借鉴 | obj-2 情感真实感 | 记忆溯源 / 抗漂移 / 治复读 |
   | #9 persona-bench v2 短板 | obj-2 情感真实感 | 短板是空回复 / 盘问化，属回复质量 |

5. 3 张孤儿票归位：`#16` embedding provider · `568` 跨对话总结 · `564` 身份触达链
