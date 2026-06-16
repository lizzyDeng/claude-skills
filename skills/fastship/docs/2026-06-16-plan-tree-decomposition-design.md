# fastship Plan 树化:执行层 context 去单体化

- 日期: 2026-06-16
- 状态: draft(待 review)
- 范围: fastship 引擎(`claude-skills/skills/fastship`)
- 来源: 与用户协同 brainstorm(本 session)

## 问题(窄口径)

fastship Phase 1 产出**单文件** `plan.md`。真实体量(本仓库已跑样本):大 feature 落在 50–95KB,最大 148KB(`persona-bench`)≈ 1.3 万–3.7 万 token。

Phase 2 执行驱动为了做依赖感知扇出,**必须把整份 plan 读进一个 context**。结果:还没写一行代码,执行侧 context 已被 plan 自身占掉一两万 token → LLM 降智(表现为执行漂移 / 整体平庸)。

这是**执行层的 context 体量问题**。与"plan 写得好不好"无关——本 spec **不**处理 Phase 1 authoring 自身的 context(单独议题,明确不在范围)。

## 根因:打包形态,不是规划范式

读真实 plan(`2026-06-09-her-loops-hardening.md`,51.6KB / 885 行),其结构**已经是一棵被压平成单文件的树**:

| 层 | 现状位置 | 体量 |
|---|---|---|
| root 共享层 | `## Locked design decisions` + `## AC contract` + `## Verified symbol signatures`(接口!) + `## File structure`(L15–65) | ~3KB,稳定,全 task 共享 |
| 叶子 | `### Task 1..9`,各 60–208 行 | 3–10KB/个,天然原子单元 |
| 边 | 末尾 `ac_mapping` JSON 块 | AC→tasks→e2e(已机器可验) |

树已经在那,只是以 51KB 单 blob 发给执行层。**病根是打包,不是计划本身。**

## 不变量

> 执行侧任何单个 context 都不得持有全量 plan——驱动线程不持有,叶子 subagent 不持有。

(推论:**禁止"执行开始时再拆"**——那个 decompose 步本身要把整 blob 读进一个 context,正是要消除的浪费。拆包必须在到达执行层之前完成,且由**确定性代码**做,不占 LLM context。)

## 现状 schema(读真代码 `orchestrator.py`)

`ac_mapping` 块(指令 L2200;`validate_plan` L1076 → `_check_plan_mapping` L1709):

```json
{"ac_mapping": [{"ac_id": "<1A AC id>", "tasks": ["实现任务1"], "e2e": ["scenario1"]}],
 "exclusive_forks": [{"id": "tf-1", "decision": "...", "status": "open"}]}
```

缺口:
- `tasks` 是**自由字符串**,与 `### Task N:` 段落**无机器绑定**;
- **无 task id、无 task→task 依赖、无 inputs/outputs**;
- 校验只查 AC→task+e2e 覆盖,不查节点图。

## 设计

### 1. node 契约(扩 `ac_mapping`,不重写)

在同一 JSON 块加 `nodes[]`,每个 task 升为带 id 的一等节点:

```json
{"nodes": [
  {"id": "task-1", "title": "HerChatDeps gains agent_loop deps",
   "deps": [], "inputs": ["root:HerChatDeps"], "outputs": ["sym:agent_loop_handle"],
   "files": ["services/.../her_chat.rs"]}
],
 "ac_mapping": [{"ac_id": "HAC1", "tasks": ["task-1"], "e2e": ["her_loops"]}],
 "exclusive_forks": []}
```

- `ac_mapping[].tasks` 改为**引用 node id**(不再自由字符串)。
- `inputs` 每项要么是 `root:<符号>`(root 层声明的接口/类型/约定),要么是某**上游**节点 `outputs` 里的项。
- `outputs` 是本节点产出、供下游消费的符号。
- 1B 本就列了 Verified signatures + 每个 task 干什么,信息已有,**只差显式声明边**。

### 2. orchestrator 拆包(确定性代码,零 LLM context)

`validate_plan` 通过后,新增 split 步(挨着现有 `attach_plan_html` L264):

- `root.md` ← Locked decisions + AC contract + Verified signatures + File structure
- `nodes/<id>.md` ← 对应 `### Task N:` 段落正文
- `skeleton.json` ← `nodes[]` 的 id/deps/inputs/outputs/files + ac_mapping(**无正文**)

人读的 `plan.md` / `plan.html` 作为 authored 主稿**原样保留**;root/nodes/skeleton 是**派生物**(降低风险:authoring 格式几乎不变,只多声明几个字段)。

### 3. 新校验 `_check_plan_node_graph`(仿 `_check_plan_mapping` 风格)

纯函数 `(ok, msg)`,FAIL on:

- 结构:`nodes` 非非空 list / 项缺 id;
- 重复 node id;
- 悬空依赖:`deps` 指向不存在的 node;
- 环:`deps` 有环(拓扑排不出);
- **悬空 input**:某 `input` 既不是 root 声明符号、也不是任何上游节点的 `output`;
- `ac_mapping.tasks` 引用了不存在的 node id。

挂进 `validate_plan`(non-bugfix),与 AC 覆盖同档**硬 gate,不等 codex**。

### 4. Phase 2 执行(走树)

🔴 **隔离靠"指针派发 + 预拼 brief",不靠驱动自觉**——光有 `nodes/<id>.md` 文件 ≠ 隔离。
现状(SKILL.md L199:"扇出由 Claude 读 plan 决定"):驱动读整份 plan,subagent 拿到的是驱动塞进 `agent(prompt)` 的任意内容,无任何机制保证"只拿自己那片"。改成:

- **驱动只 load `skeleton.json`**(几 KB,id/deps/contracts),按拓扑序扇出。驱动**绝不读 node 正文**——一旦它逐个读 `nodes/<id>.md` 拼 prompt,N 片正文全累进驱动 context → 驱动原地复爆回满 plan。驱动只**派指针**(node id + brief 路径)。
- **orchestrator 预拼 brief(确定性代码,零 LLM context)**:为每 node 生成 `briefs/<id>.md` = `root.md` + 本 `nodes/<id>.md` + 解析后的 `deps` 节点 `outputs` 契约。workflow 只下达「执行 `briefs/<id>.md`」。
- **subagent 自己读** `briefs/<id>.md`(单一自包含文件)。**有界是构造出来的,不是约定出来的。**
- 上游完成后,其 outputs(声明 + 实际产出符号)回填进下游 brief 文件。
- ⚠️ **「子 plan 有界 ≠ 代码世界受限」**:subagent 照常 read/grep 仓库源码(root.md 带 verified signatures),只是不背其它 node 的 plan 正文。

**门禁强度(诚实声明):** Phase 2 无 hook(同 model:opus,L119 仅 instruction 级"确保")。本节是 **instruction 级 + 结构兜底**(预拼 brief 使"只读一份"成为阻力最小默认路径),**非硬门禁**。要硬门禁须给 Phase 2 加 hook 校验每个 agent 输入不含全 plan——更大改动,见未决 5。

**效果(her-loops 实测体量):驱动 51KB → ~2KB;每 leaf brief 恒 ~6–14KB,不随 feature 体量涨。**

## 唯一硬骨头:契约完整性

叶子缺不缺 context,全看 1B 的 `inputs`/`outputs` 有没有抓全 task 间真实数据依赖。漏一条 → 叶子缺料。

缓解:`_check_plan_node_graph` 机器查"无悬空 input" + codex 1.5c 加审"节点图是否漏声明跨 task 依赖"。**与今天审 AC 覆盖是同一套纪律,非新风险类别。**

## 范围与 YAGNI

- **不碰 Phase 1 authoring 的 context**(用户明确另议)。
- **不加人工确认点**(保持全自动)。
- bugfix 路径无 `ac_mapping` / 无 1A → 无 `nodes`,split 跳过,**行为不变**。
- **不做 size gate**:拆包是确定性代码、零成本;小 feature(1–2 task)skeleton ≈ 整 plan,负担可忽略;统一处理,避免分支特判(策略一致性 > 微优化)。

## 验证

- 单测:`_check_plan_node_graph` 每个 FAIL 分支 + 一个全绿样例。
- split 幂等:同 plan 重跑产出 byte 一致。
- e2e(`skills/fastship/e2e`):跑一个含 deps 的 plan → 断言 `skeleton.json` 结构 + 每个 `nodes/<id>.md` 自包含(不含兄弟正文) + 驱动 brief 不含全 plan。
- 体量回归:assert 驱动 context 输入 < 阈值(如 5KB),与 plan 体量解耦。

## 未决(交 writing-plans 阶段细化)

1. `root.md` 的精确切分锚点:按 `##` 标题白名单 vs 在 plan 里下显式标记。
2. `outputs` 的"实际产出符号"如何回填:节点完成后由 subagent 申报 vs orchestrator 静态取声明。
3. `skeleton.json` 与现有 `[FASTSHIP_GOAL]` 状态行 / `cmd_goal`(L4051)的衔接。
4. 1B 指令(L2191 起)如何要求总结者产 `nodes[]` 图——措辞与硬约束。
5. 是否给 Phase 2 加 hook,把"每个 agent 输入不含全 plan"从 instruction 级 + 结构兜底升成**硬门禁**(现 Phase 2 无 hook)。
