---
name: conductor
description: "Model-tiering orchestration: a cheap main-loop model (Fable) conducts a dynamic workflow and routes reasoning-heavy leaves to Opus. Zero-gate peer of fastship — doubles as the test harness for Fable-as-orchestrator. Triggers: conductor, 分档编排, fable 编排, tiered workflow."
---

# /conductor — 分档编排（便宜模型指挥 · Opus 主奏）

心法来源（community tip，原文三条）：

1. 主模型设为 **Fable 5**（编排是高 token 的机械活，用便宜档省额度）
2. **Reasoning 开 Max**
3. 跑 **dynamic workflow**：Fable 当 orchestrator，**推理重的叶子全部 Opus**

## 定位（与 fastship 平级、互补）

| | fastship | conductor |
|---|---|---|
| 驱动 | Python 状态机，artifact 硬验证 | 纯指令，**零门禁零 hook** |
| 计划 | Phase 1 九步（法庭 + grill + codex）硬验证 | **单 plan 叶 + 单 grill 叶（均 Opus，指令级）** |
| 适用 | 重交付开发需求（要全量 E2E 证据） | 轻中量需求 / bugfix / 研究审计扫描——可一次编排完的任务 |
| 第二身份 | — | **Fable 编排能力的测试载体**：没有门禁兜底，编排质量全暴露在 ledger 里 |

🔴 重交付的功能开发仍走 fastship。conductor 不替代它。

## Step 0 — 启动自检（必做，结果写进 ledger）

1. **报告自己的模型身份**（你的 system prompt 里有 "You are powered by ..."，原样抄录）。
   - 主模型 = Fable → 本次运行是**实验组**（心法的本来形态）。
   - 主模型 = Opus/Sonnet → 照常运行，本次自动成为**对照组基线**，在 ledger 标注。
2. 提醒（不阻断）：心法要求 Reasoning Max → 用户输入 `/effort max`（session-only，
   不可持久化；Fable 每个新 session 默认回落 `high`，**每次实验开场都要重设**）。skill 无法代设。
3. 创建 run ledger：`.claude/conductor/runs/<task-slug>.md`（模板见下），先写入
   模型身份 + 任务原文 + 开始时间（用 `date` 命令取，不要编造）。

## 流程（顺序执行，禁止合并/跳步）

### 1. 分类 + 拆解
先把任务分类为 **`需求`**（新功能 / 改行为 / UI 变更）、**`bugfix`**（现有行为坏了）
或 **`审计/研究`**（纯读、产出报告），分类写进 ledger。

🔴 **方案合成本身就是 `判断`**——主循环是便宜档时禁止闭门自产方案。动代码的任务
（`需求` / `bugfix`）必须由两个 Opus 叶子完成拆解，主循环只调度与转述（指挥不主奏）：

1. **plan 叶**（`model:'opus'`）：产出 方案 + 步骤清单 + **验收标准**（可执行命令 /
   可观察行为，执行前锁定——这就是本次交付的 E2E 定义）。`bugfix` 的 plan 必须以
   「复现 + 根因定位」开头，验收标准 = 复现用例由红转绿；禁止跳根因修症状。
2. **grill 叶**（`model:'opus'`）：对 plan 对抗拷问——漏洞、边界、被忽略的更优解、
   验收标准是否真能证明交付。按结论修订 plan 后拆解才算完成。

`审计/研究` 类可由主循环直接拆解（产出是报告，错了重跑代价低），验收标准 =
覆盖完整 + 无静默截断。

步骤清单**每一步标注 `判断` 或 `机械`**（rubric 见下）。plan 原文、grill 修订记录、
验收标准全部写进 ledger。

### 1.5 方案确认（⏸️ 仅 `需求` 类）
🔴 `需求` 类拆解完成后**必须停下来等用户确认方案，确认前禁止进入 Step 2**。给出：

- 对需求的理解（一段话）+ 推荐方案与理由（💡 建议 / 📝 理由）+ 步骤清单 + 影响面
  + **验收标准**——用户确认的是「方案 + 验收」的整体，验收标准确认后即锁定。
- **涉及前端 UI 的需求加一档**：必须给出具体 UI 方案——改哪些页面/组件、交互流程、
  布局与视觉处理（文字描述或 ASCII 草图）；有多个合理做法时列 2-3 个选项让用户挑。
- 用户确认/修正后，把确认结论原文记进 ledger，再进 Step 2。

`bugfix` 类不暂停，直接进 Step 2（根因纪律已在 Step 1 强制）。

### 2. 编排
写**一个** Workflow（ultracode dynamic workflow）脚本完成全部步骤。硬规则：

- 🔴 **每一个 `agent()` 调用必须显式传 `model:`，禁止省略**。`判断` 步 → `model: 'opus'`；
  `机械` 步 → `model: 'haiku'`（或继承便宜主档，但仍须显式写出）。
- 🔴 **barrier（`parallel` 衔接两段）必须附一行书面理由**（写在脚本注释里）：下游为什么需要
  上游**全部**结果（join/dedup/early-exit）。说不出理由 = 用 `pipeline`。
- 结构化产出的叶子必须给 `schema`。
- 有截断/采样/上限 → `log()` 出被丢弃的部分，禁止静默截断。

### 3. 执行
跑 Workflow。把最终脚本原文 + 每个 `agent()` 的路由表（label → 档位 → 判断/机械）抄进 ledger。

### 4. 验收
1. 对照 Step 1 清单逐条核对产出，缺一条 = 回 Step 2 补，不许口头带过。
2. 动代码的任务：**逐条执行 Step 1 锁定的验收标准，真实命令输出原文贴进 ledger**。
   没有输出 = 没有验收，禁止"应该可以了"。`bugfix` 必须展示复现用例红 → 绿。

验收结论写进 ledger。

### 5. 复盘（scorecard）
在 ledger 末尾自评下表，逐项给 ✅/❌ + 一行证据：

| 维度 | 检查 |
|---|---|
| 完整性 | Step 1 清单是否全部覆盖，无静默砍项 |
| 档位纪律 | 是否每个 agent() 都显式标了 model，判断步无一漏配 opus |
| barrier 纪律 | 每个 barrier 是否有站得住的理由；该 pipeline 的地方没滥用 barrier |
| 扇出轴 | 拆分维度是否合理（按文件/按表/按维度…），有没有更优轴 |
| 暂停纪律 | `需求` 类是否在 1.5 真正暂停等确认；UI 需求是否给了具体 UI 方案 |
| 计划纪律 | 动代码任务是否真派了 Opus plan 叶 + grill 叶（而非主循环闭门自产方案） |
| 验收闭环 | 验收标准是否执行前锁定；Step 4 是否附真实命令输出（bugfix：红 → 绿） |
| 流程忠实度 | 各步是否照走，有无跳步/合并/自创步骤 |

## 分档 rubric（判断 vs 机械）

| `判断` → opus | `机械` → haiku/便宜档 |
|---|---|
| 需求歧义裁决、方案合成 | 枚举 / 搜索 / grep 扫描 |
| 实现（写代码）、review / grill | 格式转换、按精确 spec 套改 |
| 根因分析、冲突结论的仲裁与合成 | 收集 / 罗列 / 跑命令并转述输出 |

边界情形按"答错的代价"判：答错要返工或误导决策 → `判断`。

## Run Ledger 模板

```markdown
# conductor run: <task-slug>
- orchestrator model: <抄 system prompt 原文>
- arm: 实验组(fable) | 对照组(<model>)
- type: 需求 | bugfix
- task: <用户原文>
- started: <date 输出>

## 拆解清单
| # | 步骤 | 判断/机械 | 档位 |

## Plan（动代码的任务）
<plan 叶产出原文 + grill 拷问结论与修订>

## 验收标准（执行前锁定）
<可执行命令 / 可观察行为清单>

## 方案确认（仅需求类）
<提给用户的方案要点 + 用户确认/修正原文>

## Workflow 脚本
<最终脚本原文>

## 路由表
| agent label | model | 判断/机械 | barrier? 理由 |

## 验收
<逐条核对>

## Scorecard
<5 维表 + 证据>
```

## 实验协议（测 Fable 编排能力时）

1. **同一任务跑两臂**：主模型 Fable 跑一遍、Opus 跑一遍（`/model` 切换，各开新 session），
   各自产出 ledger。
2. **对比维度** = scorecard 5 项 + token 消耗 + 墙钟时间。
3. **已知失败模式**（评审 Fable ledger 时重点盯）：跳步/合并步骤、agent() 漏标 model、
   判断步配了便宜档、**便宜档主循环闭门自产 plan（没派 Opus plan/grill 叶）**、
   全 barrier 或全 pipeline（不做区分）、静默截断、**验收标准事后补写 / 无真实输出**、
   ledger 字段缺漏、**`需求` 类不在 1.5 暂停自顾自跑**（历史上最高频的违规，重点验）。
4. 基准任务（两臂都没跑过的新任务亦可）：
   > 审计一个 Rust+SQLx 代码库的 SQL 列漂移：找出所有硬编码列清单的 SELECT，
   > 从 migrations 推导各表现行 schema，比对并报告每处漂移（file:line、表、缺/多/错序列、严重度）。
   该任务自带一个天然 barrier 陷阱（SQL 侧 ⨝ schema 侧必须 join），能区分编排判断力。
