---
name: fastship
description: "Result-driven development skill. Brainstorm with user to define requirements and E2E verification, then execute autonomously in git worktree with verification loop. Use for any feature, bugfix, or refactor regardless of complexity."
---

# /fastship — 结果驱动开发

以 E2E 验证通过为唯一交付标准。Brainstorm 对齐目标 → 自主执行 → 验证结果 → 不对就重来。

## 适用场景

任何目标可定义的开发需求，不限复杂度：Feature / Bugfix / 重构 / 优化。

## 三阶段流程

```
阶段 1：Brainstorm + Plan + Grill
  └── 1.0:         🔴 需求分类（classify --type bugfix|feature|refactor|optimize）→ bugfix 自动激活诊断 Gate
  └── 1.1a:        读静态上下文（ARCHITECTURE / CLAUDE / git log）
  └── 1.1a-recall: 🔴 跨 session 学习 — knowledge_recall CLI 检索所有 KNOWLEDGE.md
  └── 1.1b:        🔴 并行派 ≥3 个 Explore subagent（涉及模块 / 测试 / 历史变更）
  └── 1.1c:        聚合成 Context Brief（带 file_path 引用 + recall 命中原文）
  └── 1.1d:        🔴 Bug 诊断 Gate（Bugfix 强制：复现 → 根因证据链 → 修复假设验证）
  └── 1.2~1.3: 在 Brief 上起草 AC/E2E/影响范围（不单独求确认）
  └── 1.4:    writing-plans 写实施计划
  └── 1.5:    grill-me 拷问 plan（先拷问 Brief 扎实度，再 AC / E2E / 任务拆分 / 边界 / 风险）
  └── 1.6:    ⏸️ 用户一次性确认 AC + E2E + Plan（Stage 1 唯一确认关卡）
阶段 2：Execution（executing-plans 按计划执行，禁止主线程自由拆步骤）
阶段 3：Verification Loop（E2E 验证，失败则根因分析 → 重试或回阶段 1）
```

🔴 **计划强制由 superpowers 的 skill 产出**，不让主线程自己拍脑袋拆步骤 —— 写用 `writing-plans`，执行用 `executing-plans`（有 subagent 时换成 `subagent-driven-development`）。

🔴 **Plan 必须经过 `grill-me` 的结构化拷问**再交付用户确认，杜绝"看起来合理就 ship"。

🔴 **主线程禁止亲自 grep / find / 大面积 Read 代码做调研**，统一改为 1.1b 并行派 Explore subagent。串行摸代码是 agentic 程度低的最大病灶。

🔴 **每次 fastship session 起步必须跑 `knowledge_recall` CLI**（1.1a-recall），跨 session 学习不靠 LLM 自觉去 grep —— hook Gate B 强制：未 recall 编辑代码直接 BLOCK。

---

## 阶段 1：Brainstorm + Plan + Grill

### 1.0 需求分类（🔴 强制第一步，Gate 拦未分类）

**收到用户需求后，做任何事之前**，必须先分类需求类型并调用 CLI 注册：

```bash
python3 .claude/hooks/ship_verify_gate.py classify --type <bugfix|feature|refactor|optimize>
```

| 类型 | 信号 | 后续影响 |
|------|------|----------|
| bugfix | 用户报告预期外行为、报错、数据不对、线上问题、403/500 | 🔴 自动激活 Bug 诊断 Gate（1.1d），编辑代码前必须完成 D1→D2→D3 |
| feature | 新增功能、新页面、新端点 | 正常流程 |
| refactor | 重构、优化结构、统一规范 | 正常流程 |
| optimize | 性能优化、体验优化 | 正常流程 |

🔴 **未分类就进入 1.1a → Gate 拦截编辑代码**（hook 检测 `request_classified=false` → BLOCK）。
🔴 **分类为 bugfix 后不走 1.1d 诊断 → Gate 拦截编辑代码**（hook 检测 `bug_diagnosis_done=false` → BLOCK）。
🔴 **禁止把 bugfix 标为 feature 来绕过诊断 Gate**。用户说"报错/不对/403/数据错误"= bugfix，不能降级。

### 1.1 项目上下文注入（🔴 强制，必须在回复用户之前完成）

不靠主线程一边和用户聊一边 grep 摸鱼。这一步必须**先读静态上下文 → 再并行派 Explore subagent → 最后聚合成 Context Brief**，三步都做完才开口和用户讨论 AC/E2E。

#### 1.1a 静态上下文（serial Read，主线程亲自读）

**在说任何话之前**（1.0 分类除外），立即用 Glob + Read 工具读取以下文件。不要跳过，不要先和用户讨论再读，不要用"我来看看代码"代替读架构文档。

```
必读文件（按顺序，用 Glob 搜索，存在即 Read 全文）：
├── ARCHITECTURE.md → 模块全貌 + 关键约束（🔴 最重要，有它就不需要大面积读代码）
│   搜索路径：**/ARCHITECTURE.md
├── CLAUDE.md → 项目约定（通常已自动加载，确认即可）
└── git log --oneline -15 → 最近变更上下文
```

**为什么必须先读 ARCHITECTURE.md**：它包含模块边界、关键约束、数据流，读完就知道改哪里、不能碰哪里。跳过它直接读代码 = 盲人摸象。

#### 1.1a-recall 跨 session 学习（🔴 强制，hook Gate B 拦）

读完静态上下文后，**立即调一次 `knowledge_recall`** —— 这是确定性 CLI，比让 LLM 自己去 grep KNOWLEDGE.md 可靠：

```bash
python3 .claude/hooks/ship_verify_gate.py knowledge_recall \
  --query "<需求一句话>" --top 5
```

工具行为：
- 递归扫描所有 `KNOWLEDGE.md`（项目根 + 任意子目录，不限层级）
- 用 query 的英文 word + 中文 bigram 与每条 entry 算重叠分
- 打印 top-N 命中条目的**完整原文** + 出处 `file:line` + 分数
- 命中 0 条也合规（新项目 / 新方向）；置 `knowledge_recall_done=true`

🔴 **不跑 recall → 编辑代码会被 Gate B 拦**（hook 强制，无法绕过）。
🔴 把 recall 输出的命中条目**verbatim** 拷进 1.1c Context Brief 的"相关历史教训"段。

#### 1.1b 需求专项探索（🔴 强制并行 Explore subagent）

读完静态上下文 + 跑过 knowledge_recall 后，主线程**禁止亲自 grep / find / 大面积 Read 代码**。改为在**单条消息里并行派 ≥3 个 `Explore` subagent**（[superpowers:dispatching-parallel-agents] 模式），各自带不同的探索焦点：

```
强制派出的 3 个 Explore subagent（KNOWLEDGE 历史教训已由 1.1a-recall 拿到，不重复）：

agent A —— 涉及模块清单
  prompt: 给定用户需求"<需求一句话>"，找出本次改动可能涉及的所有模块/文件。
          回答：每个模块的 file_path:line、责任、对外入口、下游依赖。
          thoroughness: medium

agent B —— 现有测试 / E2E 覆盖
  prompt: 找出与"<需求关键词>"相关的现有单测、集成测试、E2E 场景。
          回答：file_path:line、覆盖了什么、缺口在哪。
          thoroughness: medium

agent C —— 相关历史变更
  prompt: 用 git log + git show 找出最近 60 天内涉及 <相关路径> 的 commit 与
          PR；标出已修复 bug、近期 refactor、未完成的 TODO 注释。
          thoroughness: quick
```

🔴 必须 **同一条消息发出多个 Agent 工具调用**才能并行；分多条消息 = 串行 = 违规。
🔴 主线程在 subagent 返回前不准提任何 AC/E2E 问题给用户（不要边等边聊浪费 round-trip）。

#### 1.1c Context Brief（🔴 强制产出物）

把 4 个 subagent 的结果聚合成一份 brief，用于 1.2 起草和 1.5 grill-me 拷问：

```markdown
## Context Brief — {需求一句话}

### 涉及模块（agent A）
- {file_path:line} — {责任} — {入口} — {下游}
- ...

### 现有测试 / E2E 覆盖（agent B）
- 已覆盖：{file_path} — {覆盖范围}
- 缺口：{描述}（这些会在 1.3 E2E 设计里补上）

### 相关历史变更（agent C）
- 最近 commit：{sha7} — {一句主旨}
- 风险点：{触碰过相关代码的近期 bugfix / TODO}

### 相关历史教训（来自 1.1a-recall，verbatim 拷贝）
{把 knowledge_recall 命中条目的原文整段拷进来，包括 file:line 和 score；
 不要二次总结——原文比总结更可靠}
- 命中 0 条 → 写"recall 0 hits（新方向）"

### 影响（主线程合成）
- 涉及哪些模块（最终列表）
- 触碰保护标记 / 只读区？
- 回归风险：哪些已有功能可能受影响
- 直接影响 1.2 AC 起草的 1-2 个判断
```

🔴 **Brief 缺失 / 没有走并行 subagent / 没有引用 file_path → 1.2 主线程已经在盲撞**。grill-me（1.5）会优先拷问 Brief 是否扎实，不扎实直接打回 1.1b 重派 subagent。

### 1.1d Bug 诊断 Gate（🔴 Bugfix 场景强制，Feature/重构跳过）

当需求类型是 **Bugfix**（用户报告了预期外行为、报错、数据不对等）时，1.1c Brief 产出后**禁止直接进入 1.2 讨论根因**。必须先通过 Bug 诊断 Gate——用实际执行结果证明根因，不靠读代码猜。

```
Bug 诊断 Gate 三步（缺一不可）：

Step D1: 复现（🔴 必须有可执行的复现证据）
  ├── 跑现有相关测试（单测 + 集成测试 + E2E）→ 拿到实际报错 / 实际输出
  ├── 如果没有现成测试覆盖：写一个最小复现脚本/测试用例，跑出来
  ├── 如果是 staging/线上问题：curl/API 调用复现，贴实际响应
  ├── 🔴 禁止的"复现"：
  │     ❌ "我读了代码，逻辑上应该会报错" → 不算，必须实际跑出报错
  │     ❌ "根据用户描述推断" → 不算，必须自己亲手复现
  │     ❌ "看 git blame 觉得是这个 commit 引入的" → 不算，必须证明
  └── 产出：复现命令 + 实际输出（完整粘贴，不截断）

Step D2: 根因定位（🔴 必须有证据链，不能只有结论）
  ├── 基于 D1 的实际报错，沿调用链追踪（不是凭印象猜）
  ├── 定位到具体的 file:line，说清楚：
  │     ├── 这行代码的预期行为是什么
  │     ├── 实际行为是什么
  │     └── 为什么实际行为和预期不同
  ├── 🔴 证据链要求：
  │     ├── 如果说"A 调了 B，B 返回了错误的值" → 必须有日志/断点/测试输出证明 B 确实返回了错误值
  │     ├── 如果说"这个 SQL 查了错误的表" → 必须跑这条 SQL 拿到实际结果对比
  │     └── 如果说"JWT 里没有 role" → 必须解码一个真实 JWT 证明确实没有
  └── 产出：根因一句话 + 证据链（引用 D1 的输出 + 代码路径）

Step D3: 修复假设验证（🔴 先验证假设再写 Plan）
  ├── 提出修复方案后，用最小改动验证假设：
  │     ├── 方法 1：写一个测试用例，mock 修复后的行为，看是否 pass
  │     ├── 方法 2：本地临时改代码，重跑 D1 的复现步骤，看是否修复
  │     └── 方法 3：对于数据问题，用 SELECT 查询验证修复后的预期查询结果
  ├── 🔴 禁止的验证方式：
  │     ❌ "从代码逻辑看改完应该就好了" → 不算，必须实际跑
  │     ❌ "这个改法在另一个项目用过" → 不算，必须在当前项目验证
  └── 产出：验证命令 + 实际输出（证明修复方向正确）
```

**Bug 诊断 Gate 产出物（嵌入 Context Brief 补充段）**：

```markdown
### Bug 诊断（1.1d Gate 产出）

#### 复现（D1）
- 复现命令：`{实际执行的命令}`
- 实际输出：
  ```
  {完整粘贴，不截断}
  ```

#### 根因（D2）
- 根因：{一句话}
- 证据链：
  1. {D1 输出中的关键错误信息} →
  2. {追踪到 file:line} →
  3. {该行代码做了什么 vs 应该做什么}

#### 修复验证（D3）
- 修复假设：{一句话描述修复方案}
- 验证命令：`{实际执行的命令}`
- 验证结果：
  ```
  {完整粘贴}
  ```
- 结论：假设成立 / 假设不成立（不成立则回到 D2 重新定位）
```

🔴 **Bug 诊断红线**：
- D1 无复现证据 → 禁止进入 D2（不能凭读代码猜根因）
- D2 无证据链 → 禁止进入 D3（不能凭直觉说"应该是这个问题"）
- D3 假设不成立 → 回到 D2 重新定位（不能强行提修复 Plan）
- 三步都完成才允许进入 1.2 讨论 AC/E2E
- 🔴 **扫一眼代码就下结论 = 最严重违规**，等同于跳过整个阶段 1

### 1.2 与用户讨论（起草，不在此处确认）

🔴 进入这一步的前置是 **1.1c Context Brief 已产出**。Bugfix 场景额外要求 **1.1d Bug 诊断 Gate 三步已完成**。Brief 提供"涉及模块 / 测试缺口 / 历史风险 / 历史教训"作为 AC 起草的事实基础——主线程不再凭印象拍 AC。

🔴 这一步只起草，不开"⏸️ 用户确认 AC/E2E"关卡。AC + E2E + Plan 会在 1.6 一次性交给用户拍板。

```
讨论要产出 3 个东西（每条都要在 Brief 上有据可依）：
├── 1. AC 清单（验收标准）
│     每条 AC 必须是可客观验证的（不是"更好"这种主观描述）
│     🔴 至少 1 条 AC 必须直接对应 Brief 中"测试缺口"或"历史风险"
│
├── 2. E2E 验证方案（🔴 核心 — 怎么证明做对了）
│     与用户讨论确认验证方式，不预设：
│     ├── 后端 API → API 调用序列 + DB 状态检查
│     ├── 前端 UI → Browser E2E / 截图对比
│     ├── CLI 工具 → 命令行输入输出验证
│     ├── 库/SDK → 单元测试 + 集成测试
│     ├── 数据迁移 → SQL 查询验证
│     ├── 性能 → Benchmark
│     └── 其他 → 与用户定义
│     🔴 必须复用 Brief 中已有的测试/E2E 文件作为模板，不要从零写新框架
│
└── 3. 影响范围评估
      ├── 涉及哪些模块（直接抄 Brief"涉及模块"清单）
      ├── 是否触碰不应修改的区域（Brief 中已识别的保护标记 / 只读区）
      └── 回归风险：改动可能影响哪些已有功能（Brief 中"相关历史变更"提示）
```

### 1.3 E2E 验证方案格式

🔴 **E2E = 冒烟测试。用当前环境的真实数据，零构造，看输出。**

每个 E2E 场景必须声明以下 4 项，**用户审批时重点看 data_source**：

```python
scenarios = [
    {
        "name": "场景名称",
        "description": "验证什么",
        "data_source": "当前环境",     # 🔴 必填。只允许：
                                       #   "当前环境" — 用 DB 里已有的真实数据
                                       #   "API 创建" — 通过 API 调用产生前置数据（如发 5 条消息触发 extraction）
                                       #   🔴 禁止写 "手动 INSERT" / "SQL 构造" / "mock 数据"
        "steps": [
            {
                "action": "描述操作",   # 只能是 API 调用 / CLI 命令 / UI 操作
                "input": { ... },
                "expect": {
                    "status": "success",
                    "output_contains": ["..."],
                    "output_not_contains": ["..."],
                    "db_check": "..."   # 可选：查 DB 验证副作用（只 SELECT，不 INSERT）
                }
            },
        ],
        "repeat": 1,                   # LLM 相关场景建议 ≥2 次
        "pass_criteria": "1/1"
    }
]
```

🔴 **E2E 数据红线**：
- 验证过程中禁止直接操作 DB（INSERT / UPDATE / DELETE）来构造测试前提
- 如果需要前置状态，只能通过 API 调用产生（如发消息触发 extraction）
- DB 查询仅用于验证副作用（SELECT），不用于构造数据
- 违反此规则的 E2E 结果视为无效，hook 会拦截并警告

### 1.4 Plan Writing（🔴 强制走 superpowers/writing-plans）

起草完 AC + E2E + 影响范围后，**直接通过 Skill 工具调用 superpowers 的 `writing-plans`** 写实施计划，不要由主线程直接拆步骤/写 todo 开干，也不要在这里单独求一次"AC/E2E 确认"——AC/E2E 会在 1.6 与 Plan 一起被一次性确认。

```
调用方式：
  Skill(skill="writing-plans")
    └── 若当前环境未暴露 writing-plans，先 Skill(skill="using-superpowers")
        按其引导加载 writing-plans

产物：
  docs/superpowers/plans/YYYY-MM-DD-{feature-name}.md

计划必须包含（由 writing-plans 模板强制）：
  ├── Header: Goal / Architecture / Tech Stack
  ├── File Structure: 哪些文件 create / modify
  ├── Bite-sized Tasks: 每步 2-5 分钟（写失败测试 → 跑测试 → 最小实现 → 跑通 → commit）
  └── 每个任务的 Files + 测试命令 + 预期输出 + commit 信息

🔴 计划必须把 1.2 起草的 AC 清单和 E2E 方案直接嵌入文档（作为 Acceptance Criteria 章节），让 1.5 的 grill-me 能一次性拷问全部内容。
```

### 1.5 Plan Grilling（🔴 强制走全局 grill-me）

Plan 写完落盘后，**必须通过 Skill 工具调用全局 `grill-me`** 对计划做结构化拷问，把 AC、E2E 方案、任务拆分、边界、风险逐一钉死。这是合并双确认后唯一的内容质量保障层，不能跳过。

```
调用方式：
  Skill(skill="grill-me")
    └── 把 1.4 落盘的 plan 文件路径作为输入，让 grill-me 针对该文件提问

拷问必须覆盖（grill-me 会逐一深入）：
  ├── 🔴 Context Brief 扎实度：涉及模块清单是否齐全？是否引用了 file_path？
  │   测试缺口 / 历史风险有没有被 AC 接住？
  │   1.1a-recall 命中条目是否 verbatim 拷进 brief？是否在 AC/E2E 里反映？
  │   Brief 不扎实 → 打回 1.1a-recall / 1.1b 重做，不允许直接跳到 1.6
  ├── AC 是否客观可验证？是否有遗漏的成功条件 / 失败条件？
  ├── E2E 方案能不能真正证明 AC？data_source 是否合规（"当前环境" / "API 创建"）？
  ├── 任务拆分粒度是否符合 bite-sized（2-5 分钟）？
  ├── 是否触碰保护标记 / 已知高风险模块？
  ├── 回归风险：哪些已有功能可能受影响，是否在 E2E 里覆盖？
  └── 失败兜底：第 2/3 次 loop 失败时回退到哪里？

允许结果：
  ├── 拷问中发现 AC/E2E/Plan 漏洞 → 主线程当场修订 plan 文件 + 重跑相关问题
  └── 全部分支 resolved → 进入 1.6
```

🔴 grill-me 未跑完（仍有 unresolved branch）→ 禁止进入 1.6。

### 1.6 ⏸️ 用户最终确认（AC + E2E + Plan，一次性）

grill-me 跑完后，向用户输出：
- 计划落盘路径
- 拷问中被修订的关键点摘要（让用户能聚焦在变化上）
- AC + E2E + Plan 任务摘要

等待用户明确确认。**这是 Stage 1 唯一的确认关卡**，未经确认禁止进入阶段 2。

---

## 阶段 2：Execution

### 2.1 开发环境

根据需求规模选择开发方式（不强制 worktree）：

```
├── 大特性 / 高风险改动 → 推荐 worktree 隔离（分支命名：fastship/{feature-short-name}）
├── 中等改动 → 新分支即可
└── 小改动 / bugfix → 当前分支直接开发也可以
```

用户可以指定，如果没指定则由你根据影响范围判断。

### 2.2 按计划执行（🔴 强制走 superpowers skill）

执行阶段 **必须通过 Skill 工具调用 superpowers 的执行 skill**，禁止主线程凭直觉直接写代码：

```
有 subagent 环境（Claude Code 等）：
  Skill(skill="subagent-driven-development")
    └── 每个任务派发独立 subagent + 两段式 review

无 subagent 环境：
  Skill(skill="executing-plans")
    └── 加载 1.4 产出 + 1.5 grill-me 拷问后定稿的计划文件
    └── 逐个任务按 bite-sized 步骤执行（写测试 → 跑失败 → 实现 → 跑通 → commit）
    └── 每步都跑计划里声明的 verification 命令
```

执行时遵循的原则由计划和 superpowers skill 共同决定，fastship 不再重复规定。

**遇到以下情况必须停下**（executing-plans 的约束）：
- 计划有关键缺口
- 连续验证失败
- 指令含糊无法判断
- → 回到阶段 1 与用户对齐，而不是猜着改计划

### 2.3 底线 Gate（自动检测）

执行过程中的自动保护（根据项目技术栈自动适配）：

```
├── 编译检查 — 编辑源文件后自动检查（cargo check / tsc / go vet 等）
├── 保护标记 — 禁止修改带 REMOVED/bugfix/read-only 注释的代码段
└── 变更感知 — 编辑核心模块时，查看该文件最近 commit 上下文
```

### 2.4 🔴 Hook 自动 Gate（无法绕过）

代码改完后，**必须先跑通项目测试和 E2E 才能进入阶段 3**。

这不是靠 LLM 自觉，而是 **hook 自动拦截**：

```
五层自动 Gate（pre_bash hook 自动触发，exit 1 阻断）：

Gate 1: 想跑 E2E？→ hook 自动检查单测是否通过
  ├── 单测已通过 → 放行
  └── 单测未通过 → 🔴 BLOCKED，必须先跑单测

Gate 2: 想跑 E2E Gate？→ hook 自动检查单测 + E2E Runner 是否都完成
  ├── 都完成 → 放行
  └── 缺任一项 → 🔴 BLOCKED

Gate 3: 想 merge/push？→ hook 自动检查全部验证完成
  ├── 全部完成 → 放行
  └── 缺任一项 → 🔴 BLOCKED

Gate 4: 想 merge/push？→ hook 自动检查 KNOWLEDGE.md 是否表态（详见 3.2.1）
  ├── 已编辑 KNOWLEDGE.md / 已显式 knowledge_skip → 放行
  └── 未表态 → 🔴 BLOCKED

Gate 5: 想重跑 E2E？→ hook 自动检查上一轮是否已 loop_record（详见 3.2.2）
  ├── 已记录 + 失败时已写 reflection（≥200B）→ 放行
  ├── 未 loop_record → 🔴 BLOCKED，必须先记录
  └── loop_count ≥ 3 且最后一轮 fail → 🔴 锁死，必须 reset 才能重启
```

**执行顺序被 hook 强制为**：项目测试 → E2E Runner → loop_record（每轮）→ E2E Gate → KNOWLEDGE 闭环 → merge/push。
跳过任何一步，后续步骤会被 hook 自动阻断。需要手动调用 gate 脚本的两处：(a) 失败时 `loop_record --outcome fail --reflection <path>`；(b) 无新教训时 `knowledge_skip --reason "..."`。

---

## 阶段 3：Verification Loop

### 3.1 验证执行

```
Step 0: 冒烟测试（🔴 强制，在 E2E 场景之前）
  ├── 零 setup：不 INSERT，不 UPDATE，不手动构造数据
  ├── 启动服务 → 通过 API 发一条真实请求
  ├── 等 async 处理完成（extraction / webhook / queue 等）
  ├── 查 DB（SELECT）验证每个中间产物：存在？格式正确？
  ├── 任一环节失败 → 停下修，禁止进入 E2E
  └── 🔴 冒烟测试过程中执行 DB 写入 → hook 拦截

Step 1: 项目测试（全量）
  ├── 通过 → 继续
  └── 失败 → 修复后重跑（不算 loop 次数）

Step 2: E2E 验证（Runner 采集 → LLM 审查 → 质量报告 → Gate 检查）

  Step 2a: 场景设计（在阶段 1 brainstorm 时完成）
    ├── 写场景 JSON 到 tests/e2e_scenarios/
    ├── 🔴 最少 10 轮独立调用（不是 2-3 轮就够）
    ├── 🔴 必须覆盖不同的输入变体（不能只测一种表述）
    ├── 🔴 必须包含用户曾经发现的 bug 的复现场景
    └── 🔴 如涉及 LLM 输出，必须用不同上下文覆盖

  Step 2b: Runner 采集数据
    ├── 启动被测服务
    ├── 运行 E2E Runner（编排场景 + 采集响应数据）
    │   输出结构化 JSON 到 /tmp/e2e_result.json
    └── 🔴 Runner 只做编排 + 数据采集，不做任何判断

  Step 2c: LLM 审查 + 质量报告（🔴 必须输出）
    ├── 读取 /tmp/e2e_result.json
    ├── 逐轮审查：对每个操作，检查输入输出是否符合逻辑
    ├── 逐条核对阶段 1 定义的 AC
    └── 输出质量检测报告（格式见 3.1.1）

  Step 2d: E2E Gate（🔴 代码层强制，防止 LLM 偷懒）
    ├── 验证数据充分性（文件存在、≥10 轮调用、不能太旧）
    ├── 把原始数据直接展示给用户（用户对照 LLM 报告看是否诚实）
    └── Gate FAIL → 禁止合入

    🔴 合并前必须跑：
    python3 tests/e2e_gate.py --result /tmp/e2e_result.json --min-turns 10

Step 3: 回归检查
  ├── 项目测试已在 Step 1 覆盖
  └── 如架构文档标注了涉及模块的已知风险 → 额外关注
```

### 3.1.1 🔴 E2E 质量检测报告

每次 E2E 完成后必须输出。目的：防止 LLM 偷懒。

```markdown
## E2E 质量检测报告

### 覆盖度
- 总调用轮数：N
- 覆盖的场景数：M
- 覆盖的 AC 条目：列出每条 AC 对应的测试轮次

### 逐轮审查

#### Case: {场景名} — Round X Turn Y
- **输入**: 原始输入
- **输出**: 实际输出（🔴 必须完整列出，不能省略）
- **关键数据**: 列出相关中间状态/pipeline 字段（按场景需要）
- **判定**: ✅ / ❌
- **原因**: 具体说明为什么通过或失败。不能只写"一致"——要说清楚哪些内容和哪些数据对应上了
- **问题定位**:（❌ 时必填）问题在哪个环节

### 总结
- N 轮中 X 轮 PASS，Y 轮 FAIL
- 通过率：X/N
- 🔴 通过率 < 80% 或任意 AC 未覆盖 → 不能合入
- 发现的问题 + 根因
- 建议的后续动作
```

🔴 **报告红线**：
- 回复原文不能省略或截断
- 不能只说"看起来没问题"——必须列出具体的对应关系
- 空回复 = ❌，无条件
- 每条 AC 必须在报告中标注覆盖状态
- FAIL 时必须定位到具体环节（不能只说"有问题"）
- 🔴 通过率不能靠减少测试轮数来提高

### 3.1.2 🔴 E2E Gate（代码层强制，防止 LLM 偷懒）

质量报告由 LLM 输出，但 LLM 可能偷懒。Gate 脚本做两件事：
1. **验证 E2E 数据充分性**（文件存在、≥10 轮调用、不能太旧）
2. **把原始数据直接展示给用户**（用户可以对照 LLM 的报告看是否诚实）

```
🔴 合并前必须跑：
python3 tests/e2e_gate.py --result /tmp/e2e_result.json --min-turns 10

Gate 输出原始数据（每轮的输入/回复/关键字段），用户对照 LLM 的质量报告判断。
Gate FAIL → 禁止合入。
```

### 3.2 结果处理

```
✅ 全部通过：
  ├── 更新架构文档（如有，涉及模块的「最近变更」section）
  ├── 🔴 KNOWLEDGE.md 闭环（hook 强制，不再是软建议，详见 3.2.1）
  ├── 输出改动总结给用户
  └── 用户手动决定是否合入 main

❌ 失败（任意一次）：
  ├── 🔴 每次失败都先写 reflection（不再是只有第 2 次才反思，详见 3.2.2）
  ├── 落盘到 docs/superpowers/plans/<plan-name>.reflections/loop-N.md
  ├── 调用 loop_record 把本轮记成 fail（hook 校验 reflection 文件存在 & ≥200B）
  └── 反思中的 Decision 字段决定走向：
      ├── continue   → 进入下一 loop（hook 自动放行 E2E 重跑）
      ├── escalate   → 回阶段 1 与用户对齐 spec/架构（先 reset）
      └── stop       → 停下，输出完整分析给用户

❌ 失败累计 = 3：
  └── 🔴 hook 强制锁死，禁止再跑 E2E。必须向用户输出聚合报告：
      ├── 3 份 reflection 的根因对照
      ├── Circle Check 总结（是否在原地打转）
      ├── 建议（方向调整 / 需要用户决策的问题）
      └── 代码保留，用户可检查
```

### 3.2.1 🔴 KNOWLEDGE.md 闭环（hook 强制 Gate 4）

E2E 通过后，**merge / push 前必须明确表态**：要么向 KNOWLEDGE.md 写一条新教训，要么显式声明本次无新教训。这是合并/推送的硬性 Gate（hook 自动拦截，无法绕过）。

**两条放行路径（任选其一）：**

```
路径 A — 有新教训：直接编辑/创建项目根 KNOWLEDGE.md
  ├── hook 监听 Edit/Write KNOWLEDGE.md → 自动置 knowledge_acknowledged=true
  └── 文件不限层级，文件名严格匹配 KNOWLEDGE.md（大小写不敏感）

路径 B — 确实无新教训：显式跳过（必须给 ≥10 字的人话原因）
  └── python3 .claude/hooks/ship_verify_gate.py knowledge_skip \
        --reason "<原因>"
     例："纯文档改动，未触及代码或行为"
        "本次只是把 1.4/1.5 重排序，无新踩坑"
```

**该写一条时（不要硬凑）：**

- 跑过的 E2E 场景里发现了非显然的边界 / 数据假设 / LLM 行为
- 调试中证伪了某个"我以为它会 X"的假设
- 用户在 grill-me 阶段否决了某个原本看上去合理的方案
- 修复了某个会让人重复踩的坑（环境、依赖、构建、配置）

**不该写的（用 knowledge_skip 跳过即可）：**

- 纯文档 / 注释 / 重命名
- 已经被 ARCHITECTURE.md 或代码自身充分表达的事实
- 凑数式总结（"今天学会了写 if-else"）

**条目格式（追加在 KNOWLEDGE.md 末尾）：**

```markdown
## YYYY-MM-DD — {一句话主题}

**场景**：触发这条教训的具体上下文（哪个模块/哪类需求/哪种环境）。

**踩到的坑 / 反直觉点**：用一句话说清楚"我以为 X，实际 Y"。

**根因**：为什么会这样（一句话即可，不要复述代码）。

**下次怎么办**：可执行的检查清单 / 模式 / 红线（让未来的自己/agent能直接照做）。

**关联**：plan 路径、PR/commit、相关 ADR（可选）。
```

🔴 hook 行为：
- `Edit/Write KNOWLEDGE.md` → 自动 `knowledge_acknowledged=true`
- `git push` / `git merge` / `git switch main` → 检查 `knowledge_acknowledged`，未表态直接 BLOCK
- `reset` 会清除该状态（新需求开始时）

### 3.2.2 🔴 Reflection in Loop（hook 强制 Gate 5）

每次 E2E loop 失败都必须落盘一份结构化反思，再调 `loop_record` 把本轮结果钉进 state；hook 拦截一切"不反思就重跑"。这把原来"第 2 次失败才根因分析"提前到了**每一轮**，杜绝在 loop 1 烧错误假设、loop 2 才幡然醒悟的浪费。

**协议（每轮 E2E 都要走完）：**

```
1. 跑 E2E Runner → 跑 E2E Gate → 输出质量报告
2. 判断本轮是 PASS 还是 FAIL
3. PASS：
     python3 .claude/hooks/ship_verify_gate.py loop_record --outcome pass
4. FAIL：
     a. 写反思到 docs/superpowers/plans/<plan-name>.reflections/loop-N.md
        （N = loop_count + 1，文件 ≥200B，结构见下）
     b. python3 .claude/hooks/ship_verify_gate.py loop_record \
          --outcome fail --reflection <上面的路径>
     c. 按 reflection 的 Decision 决定 continue / escalate / stop
5. 不调 loop_record 就再跑 E2E → hook 直接 BLOCK
```

**Reflection 文件结构（强制段落，hook 校验体积，内容质量靠 LLM 自律）：**

```markdown
# Loop {N} Reflection — {YYYY-MM-DD HH:MM}

## Hypothesis
本轮在测什么假设？为什么相信它能解决上一轮的失败？
（loop 1 的话：解释为什么相信原计划能直接跑通。）

## Observed
具体到数据点：哪条 AC 挂了、E2E 第几轮第几步、expected 与 actual 对照、
/tmp/e2e_result.json 中的关键字段。不要模糊的"看起来不太对"。

## Invalidation Verdict
- [ ] 假设被证伪（root cause 想错了，要换方向）
- [ ] 假设方向对，但实例失败（具体修复点错了，方向继续）
- [ ] 数据不足以判断

## Circle Check（loop ≥ 2 必填）
对照 loop-1..N-1 的反思：
- 同一个 root cause？同一个症状？同一个修复套路？
- 如果 YES → 必须升维（换层次：spec / 架构 / 数据 / 环境），不能再在原层次试
- 如果 NO  → 一句话说清楚本轮探索的是哪个新维度

## Next Hypothesis
下一步要试什么？**怎么证明它和前面所有尝试是不同的？**
（不同 = 改不同文件 / 修不同症状 / 升维。同一个套路重来 ≠ 不同。）

## Decision
- [ ] continue   — 进入下一 loop，按 Next Hypothesis 重跑
- [ ] escalate   — 回阶段 1 与用户对齐（spec/架构有问题，不是代码问题）
- [ ] stop       — 这条路走不通，输出完整分析给用户
```

🔴 hook 行为：
- `post_bash` 检测 E2E Runner 命令 → `e2e_runs_since_last_record++`
- 若 `e2e_runs_since_last_record ≥ 1` 时再跑 E2E → BLOCK（必须先 `loop_record`）
- `loop_record --outcome fail` 校验：reflection 文件存在 + 体积 ≥200B
- `loop_count ≥ 3` 且最后一轮 fail → 锁死，禁止再跑 E2E（必须 reset 才能重启）
- `reset` 清空所有 loop 状态（新需求开始时）

🔴 反思红线：
- 不能把多轮反思写进同一个文件（文件名按 loop-N.md 递增）
- 不能反复 copy-paste 同一份反思敷衍计数（hook 不查重，但用户在 status 输出里能看到 reflection 路径）
- Decision 必须显式勾选一项；空 Decision = 形同没反思

### 3.2.3 🟢 Parallel Hypothesis Exploration（可选，高不确定时启用）

**默认仍是 sequential loop。** 当上一轮 reflection 的 Invalidation Verdict = "假设被证伪"且存在 ≥2 个**正交**的备选假设时，可以把下一 loop 升级为并行探索——一次跑 2-3 条不同方向的 hypothesis，赢家合回主分支。

**🔴 一次并行探索 = 1 次 `loop_record`，loop_count 只 +1。** 不会因为开 parallel 把 3 次 loop 上限烧光。

**什么时候用：**

- ✅ 上一轮 hypothesis 完全错了（不是实例失败，方向就错了）
- ✅ 至少 2 个真正正交的备选（不同模块 / 不同层次 / 不同数据假设，不是同方案的微调）
- ✅ 接受 2-3x 计算开销换 1 次更广抽样
- ✅ 每个 hypothesis 都能在隔离的 worktree 跑完整 E2E（无端口冲突等）

**什么时候不用：**

- ❌ 还没跑过 sequential loop（先用 1 次便宜的摸底）
- ❌ 备选只是变量名 / 函数顺序的微调
- ❌ E2E 依赖单例服务、数据库不能并发隔离
- ❌ Budget 紧（每个 branch 都要跑一次完整 E2E）

**机制（5 步）：**

```
1. 主线程在 docs/superpowers/plans/<plan>.reflections/loop-N.parallel/ 建子目录
   每个 hypothesis 占一个文件：hypothesis-{key}.md（key 用短名，如 a / b / cache-fix）
   每份先只写 Hypothesis 段（要测的假设 + 为什么和别的不一样）

2. 主线程通过 [superpowers:using-git-worktrees] 为每个 hypothesis 创建独立 worktree
   分支名建议：fastship/loop-N-<key>

3. 单条消息并行派 N 个 subagent（[superpowers:dispatching-parallel-agents]）：
   每个 subagent 任务：
     ├── cd 进自己 worktree
     ├── 按 hypothesis 实施改动
     ├── 跑项目测试 → 跑 E2E Runner（自己 worktree 的 hook state 独立）
     ├── 把 Observed/Invalidation/Decision 段补写到对应的 hypothesis-{key}.md
     └── 汇报：pass/fail + 关键差异 + diff stat

4. 主线程聚合：
   ├── 有 ≥1 hypothesis 在自己 worktree 通过 E2E：
   │     选 winner（评判标准：最简 diff > 不触保护区 > 改动局部 > 没引依赖）
   │     调：python3 .claude/hooks/ship_verify_gate.py loop_record \
   │            --outcome pass \
   │            --reflection-dir <dir> \
   │            --winner <winner-key>
   │     把 winner worktree 的改动 merge 回主分支（standard git merge）
   │     失败 worktree 的反思保留（很可能进 KNOWLEDGE.md）
   │
   └── 全部失败：
         调：python3 .claude/hooks/ship_verify_gate.py loop_record \
                --outcome fail \
                --reflection-dir <dir>
         所有反思必须含 Circle Check（横向对照——3 条路都死在同个根因上吗？
                                        如是，下一轮必须升维）

5. 不论赢输都不要急着删失败的 worktree —— 用户 / grill 阶段可能想看实际 diff
   差异。需要时用 [superpowers:using-git-worktrees] 提供的清理路径回收。
```

**hook 校验（gate_loop_record 自动做）：**

- `--reflection-dir` 必须是已存在目录
- 目录里必须有 ≥2 份 `.md`，每份 ≥200B
- `outcome=pass` 时 `--winner <key>` 必填且必须在反思集合里
- `outcome=fail` 时无需 winner

🔴 并行红线：
- 没用真正的 git worktree 做隔离 → 多个 subagent 同时改主仓库 = 撞车作废
- hypothesis 之间不正交（"是 a 还是 a + 1"）→ 浪费并行预算，等同 1 个 loop
- 把全部 worktree 都强行 merge 回主分支（即使没赢）→ 污染历史
- 用 parallel 绕 loop 上限（"我开 3 条 parallel = 9 次尝试"）→ 上限就是 3 次 loop_record，hook 一视同仁

### 3.3 Loop 流程图

```
            ┌──────────────┐
            │ 项目测试     │
            └──────┬───────┘
                   │ pass
            ┌──────▼───────────┐
            │ E2E Runner+Gate  │  ← Gate 5 拦：未 loop_record 不让重跑
            └──────┬───────────┘
                   │
          ┌────────┴────────┐
          │                 │
        pass              fail
          │                 │
  ┌───────▼─────────┐ ┌────▼─────────────────────────────────┐
  │ loop_record     │ │ 写 reflection                         │
  │   --outcome pass│ │  ├ sequential：loop-N.md             │
  └───────┬─────────┘ │  └ parallel  ：loop-N.parallel/*.md  │
          │           │ loop_record:                          │
          │           │  ├ sequential: --outcome fail         │
          │           │  │              --reflection <path>   │
          │           │  └ parallel  : --outcome pass|fail    │
          │           │                 --reflection-dir <dir>│
          │           │                 [--winner <key>]      │
          │           └────┬─────────────────────────────────┘
  ┌───────▼─────────┐      │
  │ KNOWLEDGE.md    │      │ Decision (sequential) /
  │ 闭环（Gate 4）   │      │ Aggregate (parallel)?
  └───────┬─────────┘      ├── continue / 有 winner → 回 E2E（loop_count<3）
          │                ├── escalate → reset + 回阶段 1
          │                └── stop     → 输出聚合报告
  ┌───────▼─────────┐
  │ 用户合入 main   │   注：loop_count==3 时 hook 锁死，强制走 stop
  └─────────────────┘     （parallel = 1 次 loop_record，不烧多个 slot）
```

---

## 状态行

每次回复末尾包含：

```
🚀 /fastship | {需求简述} | 阶段：{1-Brainstorm / 2-Execution / 3-Verification} | Loop: {0/3}
```

---

## Red Flags

**禁止：**
- 🔴 未调用 `classify --type` 就进入 1.1a 或后续步骤（Gate 自动拦截编辑代码）
- 🔴 用户描述明显是 bugfix（报错/数据不对/403/线上问题）却 classify 为 feature 来绕诊断 Gate
- 未读项目上下文文件就进入 brainstorm
- 🔴 跳过 1.1a-recall（knowledge_recall CLI）就进入 1.1b / 1.2（编辑代码会被 Gate B 拦）
- 🔴 1.1b 用串行 Read/Grep 代替并行 Explore subagent（必须在同一条消息里派 ≥3 个 Agent 工具调用）
- 🔴 1.1b 进行中 / Brief 未产出就开始问用户 AC/E2E（盲撞 = 违规）
- 🔴 Context Brief 不引用 file_path、不引用具体测试 / commit / 不 verbatim 拷 recall 命中（凑数 brief = 违规）
- 🔴 在 1.2~1.4 中间额外插入"⏸️ 用户确认 AC+E2E"关卡（已合并到 1.6，多一个关卡 = 违规）
- 🔴 未调用 `writing-plans` 就进入 1.5/阶段 2（主线程自由拆步骤 = 违规）
- 🔴 未调用全局 `grill-me` 拷问 plan 就进入 1.6（跳过质量保障层 = 违规）
- 🔴 grill-me 仍有 unresolved branch 就请求用户确认 = 违规
- 🔴 未经用户在 1.6 一次性确认 AC + E2E + Plan 就进入阶段 2
- 🔴 阶段 2 不走 `executing-plans` / `subagent-driven-development`，凭直觉直接写代码
- 🔴 未跑通项目测试就执行 E2E（hook 自动阻断，无法绕过）
- 🔴 未完成 E2E Runner 就执行 E2E Gate（hook 自动阻断，无法绕过）
- 🔴 通过 E2E 后未对 KNOWLEDGE.md 表态就 merge/push（hook 自动阻断，无法绕过）
- 🔴 用空泛理由（<10 字 / 凑数）调用 `knowledge_skip` 绕过 KNOWLEDGE Gate（hook 拒收）
- 🔴 跑完一轮 E2E 不调 `loop_record` 就直接重跑（Gate 5 自动阻断）
- 🔴 失败后不写 reflection 就调 `loop_record --outcome fail`（hook 校验文件存在 & ≥200B）
- 🔴 多轮反思塞进同一文件 / loop ≥ 2 的反思缺 Circle Check 段（违反 3.2.2 协议）
- 🔴 loop 上限 (3) 触发后还想换思路重跑（必须 reset 并回阶段 1 与用户对齐）
- 🔴 并行 hypothesis 探索没用 worktree 做隔离 → 多个 subagent 撞车（违反 3.2.3）
- 🔴 用 parallel 试图绕 loop 上限（一次并行 = 1 次 loop_record，hook 不放水）
- 🔴 parallel hypothesis 之间只是同方案的微调（不正交 = 浪费并行预算）
- 在 execution 中修改带保护标记的代码
- E2E 失败后不做根因分析直接重试第 3 次
- 自我声称"已验证通过"而不实际执行 E2E
- 跳过项目测试直接跑 E2E
- 通过验证后不更新文档
- 🔴 任何需求都必须经过 E2E 验证，无论是否使用 worktree、无论改动大小
- 🔴 E2E 阶段禁止通过 psql / SQL 直接写入数据库来构造测试前提（hook 自动拦截）
- 🔴 E2E 方案中未声明 data_source 或 data_source 为手动构造 → 用户应拒绝确认
- 🔴 Bugfix 场景未完成 1.1d Bug 诊断 Gate（复现 → 根因 → 修复验证）就进入 1.2 讨论 = 违规
- 🔴 Bug 根因分析没有实际执行输出作为证据（只读代码推理）= 最严重违规
- 🔴 Bug 修复假设未经实际验证就写进 Plan = 违规
