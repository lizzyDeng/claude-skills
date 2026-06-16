# fastship Plan 树化:臃肿单文件 → 计划树

- 日期: 2026-06-16
- 状态: draft(技术细节待 codex review)
- 范围: fastship 引擎(`claude-skills/skills/fastship`)

## 需求(🔒 锁定,不再改)

把 fastship 的 plan **从一个臃肿单文件,变成一棵计划树**:

1. **每个 task 节点拿着「和自己相关的、完整丰富度的」plan 切片**——内容丰富度**一点不减**,只是按 task 切开分发。
2. **主编排器只管两件事**:整体进度 + 在 task 之间传递 input/output。它**不持有**任何 task 的 plan 正文。
3. 任何执行 context 都不持有全量 plan;每个 task 只持有自己那一片。

明确**不在本需求内**(不要在 review 时重开):不减内容丰富度;不改 Phase 1 的需求/意图捕获质量;不加人工确认点。

---

## 现状(读真代码)

- `plan.md` 是单文件,真实样本体量 50–148KB(≈ 1.3 万–3.7 万 token)。
- `ac_mapping` 契约块(指令 `orchestrator.py` L2200;`validate_plan` L1076 → `_check_plan_mapping` L1709 → `_extract_plan_mapping_gate` L1752):
  ```json
  {"ac_mapping": [{"ac_id": "<1A AC id>", "tasks": ["实现任务1"], "e2e": ["scenario1"]}],
   "exclusive_forks": [{"id": "tf-1", "decision": "...", "status": "open"}]}
  ```
  `tasks` 是**自由字符串**,与正文 `### Task N:` 段落**无机器绑定**;**无 task id / 无 deps / 无 inputs/outputs**。校验只查 AC→task+e2e 覆盖。
- Phase 2 执行(SKILL.md L199):"implement 扇出**由 Claude 读 plan 决定**" → driver 读整份 plan;subagent 拿到的是 driver 塞进 `agent(prompt)` 的任意内容,**无隔离机制**;Phase 2 **无 hook**(L119)。

真实 plan(`her-loops-hardening.md`,51.6KB)结构已经是一棵**被压平成单文件的树**:`## Locked design decisions` + `## AC contract` + `## Verified symbol signatures` + `## File structure`(共享层) + `### Task 1..9`(各 60–208 行,天然原子) + 末尾 `ac_mapping`。**树已经在那,只差按结构拆开。**

---

## 目标结构:root + 节点 DAG

| 产物 | 内容 | 谁持有 |
|---|---|---|
| `root.md` | Locked decisions + AC contract + Verified signatures + File structure(共享层,稳定) | 每个 task 节点都读 |
| `nodes/<id>.md` | = 原 `### Task N` 段落**完整正文**(丰富度不变) | 仅对应 task 的 subagent |
| `skeleton.json` | 节点 id/deps/inputs/outputs 契约 + 进度状态(**无正文**) | 仅主编排器 |
| `briefs/<id>.md` | `root.md` + 本 `nodes/<id>.md` + 解析后的 dep outputs 契约(执行用,预拼) | 仅对应 task 的 subagent |

术语精确:这是 **root 共享层 + 叶子 DAG**(节点带 `deps`,无中间层级节点)。"树"为俗称。

---

## 数据契约:扩 `ac_mapping`,不重写

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
- `inputs` 每项:`root:<符号>`(root 层声明)或某**上游**节点 `outputs` 里的项。
- `outputs`:本节点产出、供下游消费的符号。
- 1B 本就列了 Verified signatures + 每个 task 干什么,信息已有,**只差显式声明边**。`nodes[]` 是节点身份 + I/O 接线,**不改 task 正文丰富度**。

---

## 生成:确定性拆包(零 LLM context)

`validate_plan` 通过后,orchestrator(纯代码,挨着 `attach_plan_html` L264)机械拆分:

- `root.md` ← 共享章节(按 `##` 标题白名单)
- `nodes/<id>.md` ← 各 `### Task` 段落**完整正文**
- `skeleton.json` ← `nodes[]` 图 + `ac_mapping`(无正文)
- `briefs/<id>.md` ← `root.md` + 本节点 + 解析后的 dep outputs 契约

人读的 `plan.md` / `plan.html` 作为主稿**原样保留**;以上四者是**派生物**,拆包**幂等**(同 plan 重跑 byte 一致)。

> 拆包是确定性代码、不占 LLM context——因此**不存在"decomposer 把整份 plan 读进一个 LLM context"的问题**。

---

## 校验 `_check_plan_node_graph`(仿 `_check_plan_mapping` 风格)

纯函数 `(ok, msg)`,挂进 `validate_plan`(non-bugfix),硬 gate、不等 codex。FAIL on:

- 结构:`nodes` 非非空 list / 项缺 id;
- 重复 node id;
- 悬空依赖:`deps` 指向不存在的 node;
- 环:`deps` 拓扑排不出;
- 悬空 input:某 `input` 既不是 root 声明符号、也不是任何上游节点 `output`;
- `ac_mapping.tasks` 引用了不存在的 node id;
- **文件重叠无依赖边**:两节点 `files` 有交集却无 `deps` 路径相连 = FAIL(并行编辑同文件必冲突)。

---

## 执行:主编排器走树(进度 + I/O 传递)

主编排器(driver)只 load `skeleton.json`,负责**整体进度 + 在 task 间传 I/O**,按拓扑序扇出:

- driver **绝不读 node 正文**——一旦它逐个读 `nodes/<id>.md` 拼 prompt,N 片正文全累进自己 context → 复爆回满 plan。driver 只**派指针**(node id + brief 路径)。
- dispatch 每个 task = 「执行 `briefs/<id>.md`」;**subagent 自读** `briefs/<id>.md`(= 自己那份**完整** plan 切片 + root + dep 契约),并照常 read/grep 仓库源码实现。
- **I/O 传递**:上游 task 完成时返回一个小 **output manifest**(实际产出的符号/签名);orchestrator 记进 `skeleton.json`;下游 brief 用 manifest(而非静态声明)拼。
- **root 冻结**:执行期 leaf **不得**改 root 声明的接口;若实现中发现 root 接口必须改 → 升级为该子图 re-plan,而非默默改 root(否则已派发的下游 brief 按旧 root 拼)。

**门禁强度(诚实声明):** Phase 2 无 hook(同 model:opus,L119 仅 instruction 级)。本节是 **instruction 级 + 结构兜底**(预拼 brief 使"只读一份"成为阻力最小默认路径),**非硬门禁**。升硬门禁须加 Phase 2 hook,见未决。

**效果(her-loops 实测体量):** driver 51KB → ~2KB;每 leaf brief 恒 ~6–14KB,**不随 feature 体量涨**。

---

## 节点自包含纪律

机械按 `### Task N` 切片后,node 正文若有"见 Task 2 / 如前述"这类**跨节点 prose 引用**,切成独立文件即悬空。强制 1B 写 task 段**只引 root 符号、不引兄弟节点**;加 lint:node 正文出现 `Task \d` / "前述/上文" → 告警。(注:这是约束 prose 引用,不削减 task 自身实现内容的丰富度。)

---

## 不在范围

- 不减 plan 内容丰富度(node 正文 = 原 task 段落完整搬运)。
- 不改 Phase 1 需求/意图捕获质量。
- 不加人工确认点。
- bugfix 路径(无 `ac_mapping` / 无 1A)→ 无 `nodes`,拆包跳过,**行为不变**。
- 不做 size gate(拆包零成本,小 feature skeleton ≈ 整 plan,负担可忽略;统一处理避免特判)。

---

## 验证

- 单测:`_check_plan_node_graph` 每个 FAIL 分支 + 一个全绿样例;拆包幂等。
- e2e(`skills/fastship/e2e`):跑一个含 deps 的 plan → 断言 `skeleton.json` 结构 + 每个 `nodes/<id>.md` 自包含(不含兄弟正文) + driver brief 不含全 plan + driver context 输入 < 阈值(如 5KB,与 plan 体量解耦)。

---

## 未决(交实现阶段细化)

1. `root.md` 切分锚点:`##` 标题白名单 vs 在 plan 里下显式标记。
2. output manifest 的 schema。
3. `skeleton.json` 与现有 `[FASTSHIP_GOAL]` 状态行 / `cmd_goal`(L4051)的衔接。
4. 1B 指令(L2191 起)如何要求总结者产 `nodes[]` 图——措辞与硬约束。
5. 是否给 Phase 2 加 hook,把"每个 agent 输入不含全 plan"升成硬门禁。
