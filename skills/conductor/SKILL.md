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
| 计划 | Phase 1 九步（法庭 + grill + codex）硬验证 | **plan 叶 + grill 叶（均 Opus）+ codex 交叉评审 gate（指令级）** |
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

1. **plan 叶**（`model:'opus'`）：产出 方案 + 步骤清单 + **鲁棒性清单**（见下，逐项
   覆盖或写 N/A + 理由）+ **验收标准**（可执行命令 / 可观察行为，执行前锁定——这就是
   本次交付的 E2E 定义；🔴 必须含至少一条**非 happy path 验收**：失败注入 / 弱网或
   超时模拟 / 非法输入）。`bugfix` 的 plan 必须以「复现 + 根因定位」开头，验收标准 =
   复现用例由红转绿；禁止跳根因修症状。
   - 🔴 **plan 首行必须锚定 SHA**：`已完成锚点复核（worktree @ \`<40位或前12位 sha>\`，逐处 file:line 亲验）`。
     全文每一条 `file:line` 都必须是**那一版**的行号。不锚 SHA = 后面所有引用不可复验，
     Step 1.4 一律判 FAIL（历史上 130 个 run 里 1584 条 file:line 只有 2 个锚了 SHA，
     等于全部作废）。
2. **grill 叶**（`model:'opus'`）：对 plan 对抗拷问——漏洞、边界、被忽略的更优解、
   **鲁棒性清单逐项核验（重点打 N/A 项：真不适用还是偷懒）**、验收标准是否真能
   证明交付。按结论修订 plan 出 **plan v2**。
3. **codex 交叉评审**（见 Step 1.4）：对 plan v2 的**系统外**复核，PASS 才算方案定稿。

🔴 前两道都在同一条 Claude 链里（同厂同族模型），**互为盲区**。第三道存在的唯一理由
就是它在链外——所以它的输入里**不许出现 grill 的结论**（见 1.4）。

`审计/研究` 类可由主循环直接拆解（产出是报告，错了重跑代价低），验收标准 =
覆盖完整 + 无静默截断；`审计/研究` 不走 1.4。

步骤清单**每一步标注 `判断` 或 `机械`**（rubric 见下）。plan 原文、grill 修订记录、
验收标准全部写进 ledger。

### 1.4 codex 交叉评审（`需求` / `bugfix` 必做，PASS 才能进 1.5）

链外第三方复核 plan v2。**它是 conductor 唯一的系统外锚点**——前两道叶子和主循环
同厂同族，plan 说自己对、grill 说 plan 对、终审说都对，全是自述。

**审的对象 = plan v2（grill 修订后的最终版），不是 v1。** PASS 必须绑定即将交付的那
一版；审 v1 再改 plan，结论就悬空了。

🔴 **输入里不许出现 grill 的结论**。只给：任务原文 + plan v2 全文 + 锚定 SHA。
喂了 grill 的结论 = codex 被带进同一个框架，第三道就白设了。

**启动必须有界**（照抄 fastship 洞0，禁止裸起背景 codex——会阻塞在 stdin 上）：

```bash
PLAN=.claude/conductor/runs/<slug>.plan-v2.md
PLAN_SHA=$(shasum -a 256 "$PLAN" | cut -d' ' -f1)   # 🔴 plan 文件哈希 → 填 reviewed_plan_sha256
CODE_SHA=$(git rev-parse HEAD)                      # 🔴 代码锚点 → codex 用它 git show 复验 file:line
timeout 330 codex exec -s read-only "$(cat <<EOF
你是链外交叉评审。下面是一份待执行的技术方案，审它、不要执行它。

【任务原文】<用户原话>
【代码锚点】worktree @ $CODE_SHA（方案里所有 file:line 都基于这一版，可用 git show $CODE_SHA:<path> 复验）
【本文件哈希】$PLAN_SHA（原样填进 gate 的 reviewed_plan_sha256）
【方案全文】
$(cat "$PLAN")

逐项审查，只报你**亲自验证过**的问题，禁止臆测：
1. 根因：写的是根因还是症状？有没有更上游的成因被跳过？
2. 鲁棒性清单：N/A 项是真不适用还是偷懒？
3. 🔴 验收判别力：对每一条验收标准问——**如果这份方案的核心判断是错的，这条验收还会绿吗？**
   会绿 = 无判别力，进 undiscriminating_acceptance。
4. 🔴 锚点真伪：抽查方案里的 file:line（至少 5 条，全部 <5 条则全查）。
   用 git show <sha>:<path> 核对那一行是不是方案说的那个东西。对不上 = fabricated_anchors。
5. 更优解：有没有明显更简单/更彻底的做法没被评估？

末尾输出**唯一一个** json 代码块，schema 见下，不要输出别的 JSON。
EOF
)" -c 'model_reasoning_effort="high"' < /dev/null 2>/tmp/codex.err
```

或走 `/codex review` 安全路径（已自带 timeout + stdin 重定向）。

**输出契约**——写进 `.claude/conductor/runs/<slug>.codex-review.md`，必须含机器可验 JSON gate。
🔴 纯文本「GATE: PASS」无效；**解析不出 gate = FAIL（fail-closed）**：

```json
{
  "gate": "PASS",
  "reviewed_plan_sha256": "<必须等于上面算出的 $PLAN_SHA；这是 plan 文件哈希，不是代码 SHA>",
  "root_cause_reviewed": true,
  "robustness_checklist_reviewed": true,
  "acceptance_discriminating_power_reviewed": true,
  "anchors_spotchecked": 5,
  "wrong_root_cause": [],
  "undiscriminating_acceptance": [],
  "fabricated_anchors": [],
  "unconsidered_alternatives": [],
  "blockers": []
}
```

🔴 **解析处方（实测 codex-cli 0.144.4）**：
- 输出的 JSON **带 ```json 围栏**，必须**先剥围栏再解析，禁止裸解析**（#507 判分腿就是
  裸解析吃不下围栏 JSON 而 DOA）。取最后一个 ```json 块。
- codex 把内容**同时打到 stdout 和 stderr**。以 **stdout** 为准，stderr 只用于排查
  （`2>/tmp/codex.err`）。
- `timeout` 的退出码 124 = 超时，按 **FAIL（fail-closed）** 处理并重跑一次，不许当作 PASS。

**判定**：任一布尔项不是 `true`、`anchors_spotchecked` < 5（方案锚点 <5 条则 < 全部条数）、
或任一数组非空 → **FAIL**。`reviewed_plan_sha256` 对不上 → FAIL（审的不是这一版）。

**FAIL 按缺陷层回退**（照 fastship F7）：
- `wrong_root_cause` 非空 → **回 plan 叶重出方案**（根因错了，改方案修不了）
- 其余数组非空 → 修订 plan v2 → 重跑 1.4

🔴 **最多两轮**。第二轮仍 FAIL：**停下来，把 codex 的原文分歧摆给用户裁决**，
禁止主循环自行判 PASS 或"这条不重要"绕过。conductor 零门禁，这条上限是唯一的防死循环闸。

gate JSON 原文 + 轮次 + 每轮处置全部写进 ledger。

### 1.5 方案确认（⏸️ `需求` + `bugfix`，🔴 必须 1.4 PASS 之后）
🔴 **进入本步的前置 = 1.4 gate 为 PASS**。gate 不是 PASS 就没有「方案」可确认，
禁止拿一份没过评审的方案去占用用户的注意力（把评审工作转嫁给用户 = 三道叶子白跑）。
拆解完成后**必须停下来等用户确认方案，确认前禁止进入 Step 2**。给出：

- 对需求的理解（一段话）+ 推荐方案与理由（💡 建议 / 📝 理由）+ 步骤清单 + 影响面
  + **验收标准**——用户确认的是「方案 + 验收」的整体，验收标准确认后即锁定。
- **1.4 结论一行**：codex gate = PASS（第几轮过的；若前轮有 FAIL，列出被采纳的修正）。
  用户有权看到这份方案被链外审过、审出了什么。
- 🔴 **同时产出「方案摘要图」的 HTML 版**：调用 `plan-poster` skill 编好
  `.claude/plan-posters/<task-slug>.html` 后，**直接把 HTML 交付给用户**——
  `SendUserFile(files:["...html"], display:"render")`。
  - **HTML 是必出交付，不是中间产物**：方案长图在终端里缩放后常糊到读不了验收标准，
    而验收标准恰恰是这一步要用户拍板的东西；HTML 可滚动、可选中复制、字号自适应。
  - PNG 改为**可选**：需要发手机 / 存档时再跑 `render.py` 补一张，不再是必需品。
  - 🔴 plan-poster 正文「做法第 3 步 = 发 PNG」在 conductor 路径下**被本条覆盖**；
    它的**七段内容规则与样式红线照旧全部遵守**（覆盖的只是交付形态，不是内容规范）。
  - plan 叶的产出是给 agent 消费的，**这份 HTML 是给人消费的**，两个出口缺一不可；
    图上的验收标准必须与 plan 里锁定的**逐字一致**（图是投影，不是二次创作）。
  - HTML 路径（有 PNG 则一并）写进 ledger。
- **涉及前端 UI 的需求加一档**：必须给出具体 UI 方案——改哪些页面/组件、交互流程、
  布局与视觉处理（文字描述或 ASCII 草图）；有多个合理做法时列 2-3 个选项让用户挑。
  UI 方案必须含 **loading / error（带重试入口）/ empty** 三个非 success 态的处理。
- 用户确认/修正后，把确认结论原文记进 ledger，再进 Step 2。

🔴 **`bugfix` 同样暂停**（2026-08-18 裁决推翻旧版「bugfix 不暂停」）。理由：Step 1 的根因
纪律约束的是 plan 叶怎么写，不等于**根因判对了**——判错根因的修复照样能把清单填满、
把验收写绿。bugfix 要用户确认的是**根因判定 + 修法**，不是要不要做。
项目 CLAUDE.md 的「写代码前必须确认分析」对 bugfix 一视同仁，conductor 不提供豁免。
改动 < 20 行且根因已由复现用例坐实的，摘要图可降级为正文写清（plan-poster 自身的跳过档），
但**暂停等确认这一步不可跳**。

🔴 未经本步确认就跑完的实现，对外只能说「代码待审」，**禁止说「修复好了」**。

### 2. 编排
写**一个** Workflow（ultracode dynamic workflow）脚本完成全部步骤。硬规则：

- 🔴 **每一个 `agent()` 调用必须显式传 `model:`，禁止省略**。`判断` 步 → `model: 'opus'`；
  `机械` 步 → `model: 'haiku'`（或继承便宜主档，但仍须显式写出）。
- 🔴 **barrier（`parallel` 衔接两段）必须附一行书面理由**（写在脚本注释里）：下游为什么需要
  上游**全部**结果（join/dedup/early-exit）。说不出理由 = 用 `pipeline`。
- 结构化产出的叶子必须给 `schema`。
- 有截断/采样/上限 → `log()` 出被丢弃的部分，禁止静默截断。
- 🔴 动代码任务：**implement 叶的 prompt 必须内嵌 plan 鲁棒性清单中与其负责范围对应
  的条目，review 叶按同一清单逐项核验**——清单只活在 plan 里、实现叶看不见 = 白写。

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
| 暂停纪律 | `需求`/`bugfix` 是否都在 1.5 真正暂停等确认；UI 需求是否给了具体 UI 方案 |
| 摘要图 | 是否产出摘要图 **HTML** 并 `display:"render"` 发出；图上验收标准与 plan 是否逐字一致 |
| 计划纪律 | 动代码任务是否真派了 Opus plan 叶 + grill 叶（而非主循环闭门自产方案） |
| 锚点纪律 | plan 是否锚了 SHA；全文 file:line 是否都基于那一版（否则 1.4 无从复验） |
| 交叉评审 | 1.4 是否真跑了 codex（贴命令与原始输出）；gate 是否为机器可验 JSON；`reviewed_plan_sha256` 是否对得上 plan v2；输入是否**未**掺 grill 结论 |
| gate 纪律 | FAIL 是否按缺陷层回退并重跑；有没有主循环自行判 PASS / 绕过某条；两轮未过是否停下来交用户裁决 |
| pass 门 | 摘要图与用户确认是否**都发生在 gate PASS 之后**（不是拿未过审方案去占用户注意力） |
| 验收闭环 | 验收标准是否执行前锁定；Step 4 是否附真实命令输出（bugfix：红 → 绿） |
| 鲁棒性 | 清单是否逐项覆盖（N/A 有理由）；implement 叶是否带清单；验收含非 happy path |
| 流程忠实度 | 各步是否照走，有无跳步/合并/自创步骤 |

## 分档 rubric（判断 vs 机械）

| `判断` → opus | `机械` → haiku/便宜档 |
|---|---|
| 需求歧义裁决、方案合成 | 枚举 / 搜索 / grep 扫描 |
| 实现（写代码）、review / grill | 格式转换、按精确 spec 套改 |
| 根因分析、冲突结论的仲裁与合成 | 收集 / 罗列 / 跑命令并转述输出 |

边界情形按"答错的代价"判：答错要返工或误导决策 → `判断`。

## 鲁棒性清单（plan 叶必填）

LLM 写方案天然只写 happy path——所以把非 happy path 做成必填表。每项要么给出设计，
要么写 `N/A + 一句理由`；**禁止留空，留空 = 没想过 ≠ 不适用**。

| 维度 | 必须回答 |
|---|---|
| 外部调用失败 | 每个 API / DB / LLM / 文件调用失败时的行为：报错文案、降级、是否重试 |
| 弱网与超时 | 超时阈值、重试策略（次数 / 退避）、慢网与离线下的行为 |
| 输入边界 | 空值 / 超长 / 非法格式 / 重复提交（幂等、双击防抖） |
| 失败后状态 | 中途失败的回滚或恢复路径，不留半成品状态 |
| UI 四态 | 涉 UI 必填：loading / error（含重试入口）/ empty / success 四态齐全 |

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
- 锚定 SHA：<worktree HEAD>
<plan 叶产出原文 + grill 拷问结论与修订 → plan v2>

## Codex 交叉评审（1.4）
- 轮次：<第 N 轮 PASS>
- plan v2 sha256：<shasum 输出>
- 命令原文：<timeout 330 codex exec ...>
- gate JSON：<原样粘贴>
- 每轮 FAIL 条目与处置：<逐条：采纳并改了什么 / 回 plan 叶重出>

## 验收标准（执行前锁定）
<可执行命令 / 可观察行为清单>

## 方案确认（需求 + bugfix）
<提给用户的方案要点 + codex gate 结论一行 + 用户确认/修正原文>
- 方案摘要图 HTML：<.claude/plan-posters/<slug>.html>（必出）
- PNG（可选）：<.claude/plan-posters/<slug>.png>

## Workflow 脚本
<最终脚本原文>

## 路由表
| agent label | model | 判断/机械 | barrier? 理由 |

## 验收
<逐条核对>

## Scorecard
<scorecard 全表逐项 ✅/❌ + 一行证据>
```

## 实验协议（测 Fable 编排能力时）

1. **同一任务跑两臂**：主模型 Fable 跑一遍、Opus 跑一遍（`/model` 切换，各开新 session），
   各自产出 ledger。
2. **对比维度** = scorecard 全项 + token 消耗 + 墙钟时间 + **1.4 首轮 gate 是否 PASS**
   （首轮 PASS 率是 plan/grill 两道叶子质量的链外度量——它不受本条链自评影响）。
3. **已知失败模式**（评审 Fable ledger 时重点盯）：跳步/合并步骤、agent() 漏标 model、
   判断步配了便宜档、**便宜档主循环闭门自产 plan（没派 Opus plan/grill 叶）**、
   全 barrier 或全 pipeline（不做区分）、静默截断、**验收标准事后补写 / 无真实输出**、
   **鲁棒性清单留空或全 N/A 糊弄（happy-path-only plan）**、ledger 字段缺漏、
   **`需求`/`bugfix` 不在 1.5 暂停自顾自跑**（历史上最高频的违规，重点验）、
   **跳过 1.4 codex 评审**或只贴一句「codex 说没问题」（无 JSON gate = 没跑）、
   **把 grill 结论喂进 codex 输入**（第三道降级成复读第二道）、
   **gate FAIL 后主循环自行判「这条不重要」绕过**、
   **plan 不锚 SHA**（1584 条 file:line 只 2 条可复验的历史成因）、
   **只发 PNG 不发 HTML** 或在 gate PASS 之前就把方案推给用户。
4. 基准任务（两臂都没跑过的新任务亦可）：
   > 审计一个 Rust+SQLx 代码库的 SQL 列漂移：找出所有硬编码列清单的 SELECT，
   > 从 migrations 推导各表现行 schema，比对并报告每处漂移（file:line、表、缺/多/错序列、严重度）。
   该任务自带一个天然 barrier 陷阱（SQL 侧 ⨝ schema 侧必须 join），能区分编排判断力。
