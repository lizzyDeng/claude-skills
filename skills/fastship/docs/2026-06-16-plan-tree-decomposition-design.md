# fastship Plan 树化:臃肿单文件 → 计划树

- 日期: 2026-06-16
- 状态: draft v2(已过一轮 codex 技术审查 → 有条件可行,本版吸收其发现)
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
- `ac_mapping` 契约块(指令 `orchestrator.py` L2200;`validate_plan` L1076 → `_check_plan_mapping` L1737 → `_extract_plan_mapping_gate` L1752;`PLAN_MAPPING_FIELD` L1449):`tasks` 是**自由字符串**,与正文 `### Task N:` 段落**无机器绑定**;**无 task id / deps / inputs/outputs**;校验只查 AC→task+e2e 覆盖。bugfix(`request_type == bugfix`,L1100)跳过 ac_mapping。
- Phase 2 执行(SKILL.md L199:"扇出**由 Claude 读 plan 决定**";Step 2.0 指令 L2312 同):driver 读整份 plan;subagent 拿到的是 driver 塞进 `agent(prompt)` 的任意内容,**无隔离机制**;Phase 2 **无 hook**(L119)。`cmd_goal`(L4051)只输出完成条件,不含 plan 结构入口。

真实 plan(`her-loops-hardening.md`,51.6KB)结构已经是一棵**被压平成单文件的树**(共享章节 + `### Task 1..9` + 末尾 `ac_mapping`)。**树已经在那,只差按结构拆开并接进执行/门禁。**

---

## 目标结构:root + 节点 DAG

| 产物 | 内容 | 谁持有 |
|---|---|---|
| `root.md` | 共享层(设计决策 + AC 契约 + Verified signatures + File structure),稳定 | 每个 task 节点都读 |
| `nodes/<id>.md` | = 原 `### Task N` 段落**完整正文**(丰富度不变) | 仅对应 task 的 subagent |
| `skeleton.json` | 节点 id/deps/inputs/outputs 契约 + 进度状态(**无正文**) | 仅主编排器(driver) |
| `briefs/<id>.md` | `root.md` + 本 `nodes/<id>.md` + 解析后的 dep outputs 契约(执行用,预拼) | 仅对应 task 的 subagent |

术语精确:这是 **root 共享层 + 叶子 DAG**(节点带 `deps`,无中间层级节点)。"树"为俗称。

---

## 数据契约:唯一 contract block(扩 `ac_mapping`)

🔴 全文**只允许一个** fastship contract block,且必须**同时**含 `nodes` + `ac_mapping` + `exclusive_forks`(codex:现 `_extract_plan_mapping_gate` L1752 取"最后一个含 `ac_mapping` 的 JSON 块",加 `nodes[]` 后易和示例块错配;grill L1872 也复用)。**validate / split / grill 共用同一 extractor**,只认这个唯一块。

```json
{"nodes": [
  {"id": "task-1", "title": "HerChatDeps gains agent_loop deps",
   "deps": [], "inputs": ["root:HerChatDeps"], "outputs": ["sym:agent_loop_handle"],
   "files": ["services/api-server/src/her/her_chat.rs"]}
],
 "ac_mapping": [{"ac_id": "HAC1", "tasks": ["task-1"], "e2e": ["her_loops"]}],
 "exclusive_forks": []}
```

节点严格类型(全部非空):

- **id**:`^[a-z0-9][a-z0-9_-]*$` —— 它会变成 `nodes/<id>.md` 文件名,**禁** `/`、`..`、空白、控制字符。
- **outputs**:**全局唯一**(两上游产同一 `sym:x` 会让下游 input 解析歧义)。
- **files**:只允许 **repo-relative 具体文件路径**,**禁** glob / 目录 / 省略号;规范化成 canonical key 再比较。
- **inputs** 每项:`root:<符号>`(root 层声明)或某**上游**节点 `outputs` 项。
- `ac_mapping[].tasks` 改为**引用 node id**;每个 node 要么被 `ac_mapping.tasks` 引用,要么显式 `supporting_for`(否则游离 node 无覆盖归属)。

`nodes[]` 是节点身份 + I/O 接线,**不改 task 正文丰富度**。

### 迁移(codex Critical)

现 `_check_plan_mapping`(L1737)只验 `tasks/e2e` 非空字符串、1.4 指令(L2191/L2200)要自由文本、既有测试以自由文本为 passing case。迁移须**一起改**:`ac_mapping.tasks` 校验改成"必须全在 node id 集" + 新增 `_check_plan_node_graph` + 改 1.4 指令措辞 + 改测试样例;bugfix 分支继续完全跳过 node graph。

---

## 生成:`materialize_plan_tree()` —— 硬步骤、可信产物(codex Critical)

🔴 **不**挂在 `attach_plan_html`(L264:best-effort、失败不阻断、写 non-trusted)旁边——`skeleton/briefs` **驱动** Phase 2,不能像 HTML 一样丢失/陈旧/不入状态。改为:

- 独立 `materialize_plan_tree()`,在 1.4 `validate_plan` 成功后**作为硬步骤**执行;**失败 → 1.4 不通过**。
- 产物登记到**可信 artifact ledger**:`plan_tree_dir / skeleton_path / tree_hash / source_plan_sha256`(validator 重算 hash;不放进 `plan_html_path` 那类非可信字段)。
- 切分用 **fence-aware 行扫描 / CommonMark AST**,**不裸 regex 切标题**;task block 由**显式锚点** `<!-- fastship:node task-1 -->` 与 `nodes[].id` 绑定(codex High:真实 plan 同时有 `## Task` 和 `### Task`,代码围栏内还有 `## Task 1` 这类假标题)。
- `root.md` 边界用**显式标记**(`<!-- fastship:root -->` 区间)或默认"首个 node 锚点前的顶层内容全进 root",fence-aware 排除代码块内假标题(codex High:真实章节名差异大,纯标题白名单不稳定)。
- **幂等 + stale 清理**:按 `tree_hash` 输出到独立目录 或 每次先清空该 plan tree 目录(codex Medium:否则删掉的 `task-9` 残留旧 `nodes/briefs`);JSON 固定排序/缩进/末尾换行。
- 派生:`root.md` / `nodes/<id>.md` / `skeleton.json` / `briefs/<id>.md`。人读 `plan.md`/`plan.html` 主稿原样保留。

---

## 校验 `_check_plan_node_graph`(硬 gate,挂 `validate_plan` non-bugfix)

纯函数 `(ok, msg)`,不等 codex。FAIL on:

- 结构 + 严格类型:`nodes` 非非空 list;每 node 的 `id/deps/inputs/outputs/files` 类型正确且非空;`id` 匹配 `^[a-z0-9][a-z0-9_-]*$`;
- 重复 node id;**`outputs` 全局重复**;
- 悬空 `deps`(指向不存在 node);**环**(拓扑排不出);悬空 `input`(非 root 声明、非任何上游 `output`);
- `ac_mapping.tasks` 引用不存在的 node id;游离 node(既不被 `tasks` 引用、又无 `supporting_for`);
- **文件重叠无依赖边**:两 node `files`(canonical)有交集却无 `deps` 路径相连 = FAIL。

---

## Phase 2 接入点(🔴 必改——不改等于没做,codex Critical)

现状无接入点:Step 2.0 指令(L2312)与 SKILL L199 写"读 plan 自主决定扇出",`cmd_goal`(L4051)不含 skeleton 路径。必须改:

- **Step 2.0 指令 + SKILL.md L199 + `format_next`(L3246)**:改成"读 `artifacts.plan_tree.skeleton_path`,按拓扑序、**只把 `briefs/<id>.md` 路径**发给 subagent;driver **不读 node 正文**(否则逐个读会把 N 片正文累进自己 context,复爆回满 plan)"。
- **`cmd_goal`**:输出含 `skeleton_path` 入口。
- **`status` / `[FASTSHIP_GOAL]` 状态行(L3235)**:加 `nodes_total / nodes_done / nodes_failed / current_node / plan_tree_hash`,以 skeleton 状态为**唯一进度源**(codex Medium)。

---

## 执行:运行期可验证(无 hook → git diff 兜底,codex High)

Phase 2 无 hook(L119),manifest/root 冻结**不能只靠自报**:

- **driver 是 `skeleton.json` 唯一 writer**(并行同 worktree 下 subagent 自写 = JSON 竞态);manifest 按 node id 排序合并。
- 每个 node 完成:捕获**实际 git diff**,校验 `files_changed ⊆ node.files`(canonical);越界 = 该 node FAIL;manifest 绑 diff hash。
- **output manifest**:上游返回实际产出符号/签名 → driver 记进 skeleton → 下游 brief 用 manifest(不用静态声明)。
- **root 冻结**:至少校验 plan `tree_hash` 未变;接口级冻结须符号级 validator 或 Phase 2 hook(见未决)。
- **门禁强度(诚实):** instruction 级 + 结构兜底(预拼 brief)+ **git diff 运行期复核**,**非 hook 级硬门禁**。
- 效果:driver 51KB → ~2KB;每 leaf brief 恒 ~6–14KB,不随 feature 体量涨。

---

## 2.5 code-review gate 树覆盖(codex High)

现 validator(L1399)只验 `reviewed_against` 路径存在 + `reviewed_files` 与 git diff basename 相交。加:`reviewed_plan_tree_sha256` / `reviewed_node_ids` / `reviewed_manifests` / 全路径 `reviewed_files`;validator 校验**所有 required node 已 done** 且 changed files ⊆ reviewed_files。

---

## 节点自包含纪律

机械切片后,node 正文若有"见 Task 2 / 如前述"这类**跨节点 prose 引用**,切成独立文件即悬空。强制 1B 写 task 段**只引 root 符号、不引兄弟节点**。lint(`Task \d` / "前述/上文")**仅 warning**(codex Low:真实正文会自然出现 `Task 4`,硬校验靠结构化 `inputs/deps/outputs`,不靠 prose 关键词)。不削减 task 自身实现内容的丰富度。

---

## 不在范围

- 不减 plan 内容丰富度(node 正文 = 原 task 段落完整搬运)。
- 不改 Phase 1 需求/意图捕获质量。
- 不加人工确认点。
- bugfix 路径(无 `ac_mapping` / 无 1A)→ 无 `nodes`,拆包跳过,**行为不变**。
- 不做 size gate。

---

## 验证

- 单测:`_check_plan_node_graph` 每个 FAIL 分支(含 id 正则 / outputs 唯一 / 文件重叠 / 环)+ 全绿样例;`materialize_plan_tree` 幂等 + stale 清理;唯一 contract block extractor;fence-aware 切分(代码围栏内假标题不误切)。
- e2e(`skills/fastship/e2e`):含 deps 的 plan → 断言 `skeleton.json` 结构 + 每个 `nodes/<id>.md` 自包含(不含兄弟正文) + driver brief 不含全 plan + driver context 输入 < 阈值(如 5KB) + 运行期 `files_changed ⊆ node.files` 复核 + 2.5 gate 树覆盖。

---

## 未决(交实现阶段)

1. `root.md` 边界标记 / node 锚点的具体语法(`<!-- fastship:root -->` / `<!-- fastship:node <id> -->`)。
2. output manifest 的 schema。
3. 是否给 Phase 2 加 hook,把"每个 agent 输入不含全 plan" + "root 冻结"升成硬门禁(现为 instruction + 结构兜底 + git diff 兜底)。
4. `skeleton.json` 与既有 session artifact ledger / state 的具体存储形态。
