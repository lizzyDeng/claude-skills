---
name: fastship
description: "Result-driven development skill. Python orchestrator drives every step with hard validation. Works in Claude Code (hook mode) and Codex/other agents (CLI mode)."
---

# /fastship — 结果驱动开发（Python 编排版）

E2E 验证通过为唯一交付标准。Python 状态机驱动每一步，artifact 硬验证，不能跳步。

## 启动

🧠 **Context 预检**（软建议）：`start` 命令会检查最近 2 分钟内是否执行过 `/compact`。未 compact → 打印建议但**继续启动**（不阻断）；大需求前建议主动 `/compact` 保持 context 干净，是否执行由用户决定。

收到需求后立即运行：
  python3 "${CLAUDE_PLUGIN_ROOT}/skills/fastship/orchestrator.py" start "<需求>"

fastship 支持并行多个活跃需求。每个需求有独立 session/state：
`{git-dir}/fastship/sessions/<session-id>/orchestrator.json` + `gate.json`。
默认 session 由需求文本生成；需要和 Forge feature 对齐时使用 `--session <feature-slug>`。
默认 `start` 会尽力创建隔离 worktree：先同步 `staging`（若该分支已在某个 worktree checkout，则在那里 `git pull --ff-only origin staging`；否则 `git fetch origin staging`），再 `git worktree add -b fastship/<session> .claude/worktrees/<session> origin/staging`，并在该 worktree 的 `{git-dir}/fastship` 下写 session state。创建成功后必须 `cd` 到输出的 worktree 路径继续执行；创建失败且未加 `--require-worktree` 时降级为当前工作区启动并打印警告。项目可在 `.claude/fastship.project.json` 的 `git.base_branch` / `git.worktree_root` / `git.branch_prefix` / `git.worktree_mode` 覆盖默认值；一次性覆盖用 `start --base <branch> --worktree-root <path> --branch <name>`。

## 嗅探 loop（后台存活监控）

🔴 **start 成功后，驱动 agent 必须立即以后台方式自动启动嗅探 loop**（Claude Code 主路径，
用户零动作）：取 start 输出的 🕵️ hint 段里的 /loop 嗅探命令原样执行。CLI/Codex 等无后台
能力的环境降级为提示用户手动粘贴该命令。

嗅探每轮只跑一条纯 stdlib 命令（`orchestrator.py sniff`，零 LLM 判定），按其单行
`[FASTSHIP_SNIFF]` 输出行动：

- `action=resume` → 向 fastship 驱动 session 注入一次「继续当前 step」软唤醒。**绝不 kill
  任何进程**——硬卡死交给升级通知，由用户决策。
- `action=notify_user` → 立即用最醒目可用通道通知用户，原样附上整行证据链
  （signal / stalled_since / stalled_s / resume_at）。
- `verdict=session_done` / `no_session` → 流程已终结，停止本 loop。

判定信号只有两个权威源：bg job 的 state.json `state` 字段（绝不看 mtime）+ 当前 step
停留时长（等人步骤 1.5/1.6/3.5 豁免）。resume/通知按事件键 (step, 信号, 事件标识) 去重
持久化在 sniff-state.json——不同 bg job、或 step 重入刷新 entered_at 都是新事件、重新走
完整升级链；同一事件链终身 resume 一次、notify 一次，不会风暴。嗅探心跳在
`fastship status` 可见，超龄显示 ⚠️ watchdog stale。阈值与轮询间隔均可在
`.claude/fastship.project.json` 的 `sniff` 段覆盖
（`{"sniff": {"threshold_default_s": 3600, "thresholds": {"3.1": 7200}, "interval_s": 240}}`）。

🔴 **loop 存活自检（洞1）**：驱动每次 `next` / `done` / hook 推进时,orchestrator 会自动
检查嗅探 loop 心跳——未运行或超龄(>2×interval)即在输出末尾打印告警 + 可复制的 /loop
重启命令。配合洞0(子进程必有界 → 驱动必周期性活动)形成闭环:loop 死掉会在下一次驱动
活动时被发现并提示重启,不再依赖人工去看 `fastship status`。

## 双模工作方式

### Claude Code（hook 模式 — 最强）

orchestrator 是 hook 入口。每次 Edit/Write/Bash 自动触发：
- **pre_edit**: Phase 1 阻止编辑代码，打印当前步骤
- **post_edit/post_bash**: 自动检测步骤完成，推进下一步

19 步中多数步骤由 hook 自动推进，少数确认/决策步骤需手动：
  python3 "${CLAUDE_PLUGIN_ROOT}/skills/fastship/orchestrator.py" done [--flags]

### Codex / 其他 Agent（CLI 模式）

无 hook，agent 手动驱动每一步：
  1. `python3 "${CLAUDE_PLUGIN_ROOT}/skills/fastship/orchestrator.py" next` → 读当前步骤指令
  2. 执行步骤
  3. `python3 "${CLAUDE_PLUGIN_ROOT}/skills/fastship/orchestrator.py" done [--flags]` → 验证 + 推进
  4. 重复

全部 19 步需手动 done，但 protected validators 不允许 filesystem fallback。
无 hook/gate state 的关键步骤（plan provenance、Codex review、E2E、report、gate、loop pass、knowledge）必须失败，不能靠文件存在自动通过。
Codex/CLI 模式下，文件产物步骤必须显式绑定 artifact：`done --brief <path>`、`done --requirements <path>`、`done --plan <path>`、`done --grill <path>`、`done --codex-review <path>`、`done --code-review <path>`、`done --report <path>`、`done --knowledge <path>`。没有绑定就不能通过。

### 🔴 启动 codex 评审的唯一安全方式（洞0）

1.5c Codex Review 必须以**有界形式**启动,禁止裸起背景 codex(背景 codex 阻塞在
"Reading additional input from stdin" 会永不退出 → 无完成事件 → harness 永不唤醒 →
流程静坐)。canonical 形式(timeout 包裹 + stdin 接 /dev/null):

    timeout 330 codex exec -s read-only "<prompt>" -c 'model_reasoning_effort="high"' < /dev/null 2>/tmp/codex.err

或直接走 `/codex review` 安全路径(已自带 timeout+stdin 重定向)。Claude Code hook 模式下
pre_bash 会硬拦裸起 codex;CLI/Codex 模式无 hook,须靠本处方自律。

## 流程概览

```
Phase 1: Brainstorm (9 步)
  1.0  需求分类         [CC:auto | Codex:done] classify CLI
  1.1  上下文+recall    [CC:auto | Codex:done] knowledge_recall CLI
  1.2  并行 Explore     [CC:done  | Codex:done] done --agents N (≥3)
  1.3  Context Brief    [CC:auto | Codex:done] .fastship-brief.md 验证章节
  1.3r 1A 需求拷打      [CC:auto | Codex:done] 多角色法庭→书记员合成→grill→需求定稿 .fastship-requirements.md (仅 feature；bugfix 跳)
  1.3d Bug 诊断         [CC:auto | Codex:done] fix_verified (仅 bugfix)
  1.4  1B 技术方案      [CC:auto | Codex:done] RD/QA/总结者法庭→writing-plans 签名 + 每条 1A P0/P1 AC→task+E2E 映射 (bugfix 只验签名)
  1.5  Grill            [CC:auto | Codex:done] 裁决 1B open 技术 fork（须 fork_resolutions 逐条回写 resolution）；feature 无 open fork 自动跳过(F4)，bugfix 照跑
  1.5c Codex Review     [CC:auto | Codex:done] .fastship-codex-review.md PASS/FAIL
                        FAIL → 按缺陷层回退(F7：需求层 p0_requirements_missing→1.3r / 方案层→1.4) → 重走至 1.5c
  1.6  用户确认         [CC:done  | Codex:done] done --user-confirmed

Phase 2+3: /goal 自主执行（Plan 确认后自动触发）
  2.0  执行计划         [/goal 自主驱动] dynamic workflow：读 skeleton.json 拓扑扇出（≥2 不相交组 parallel，同 worktree 只编辑不 commit），每 subagent 只收 briefs/<id>.md → implement→review pipeline
  2.5  Code Review      [硬 gate] .fastship-code-review.md PASS/FAIL
                        FAIL → 修复实现后重新 review（留在 2.5，不回退 plan）
  3.0  冒烟测试         [/goal 自主驱动]
  3.1  项目测试         [/goal + hook auto-detect]
  3.2  验证意图         [/goal + hook auto-detect] verification-plan.json：逐 AC 派生可导航目标
  3.3  验证执行         [/goal + hook auto-detect] 看一眼再点：读真实 a11y 树→旅程→逐 AC 收证据 bundle+截图
  3.4  AC 裁判+结构 gate [/goal + hook auto-detect] 对抗裁判逐 AC 出证→确定性结构 gate→HTML 报告
  3.5  Loop Record      [/goal 自主决策: fail→auto continue, 3次后暂停]
  3.6  KNOWLEDGE 闭环   [/goal + hook auto-detect]
```

## /goal 自主执行（Phase 2+3）

Plan 确认后（步骤 1.6 完成），orchestrator 自动输出 `/goal` 命令。用户执行后：

- Claude 自主驱动 Phase 2（执行）+ Phase 3（验证）全流程
- Phase 2 执行走 ultracode Workflow implement→review pipeline：每个 plan task 实现完立即对抗性 review（设计稿保真度 / spec 合同 / 质量三视角），并在 2.5 合并成 code-review 硬 gate。tests 绿 ≠ 长得像设计稿
- 🔴 Phase 2 = 推理档（model tier）：2.0 implement→review pipeline 和 2.5 多视角 review 的 dynamic workflow 里，每个 `agent()` 都必须显式 `model: 'opus'`，不继承主模型。Phase 2/3 无硬 hook、整段 /goal 自主驱动，本条与"禁止并行改重叠文件"等同属 instruction 级强约束（执行力相同，非弱于其它 Phase 2 规则）。fastship 在 Phase 2 无 hook 回看 workflow 内每个 agent 的实际模型，故这是 instruction 级"确保"，非门禁级证明
- Haiku 评估器通过 `[FASTSHIP_GOAL]` 状态行判断是否完成
- 每步完成后 Claude 运行 `status`，评估器解析 `step=` / `test_passed=` / `e2e_executed=` 等字段
- Loop Record fail 时 Claude 自主选择 continue（在 3 次上限内），3 次后暂停等用户介入
- 全部完成（step=done + 三个 gate flag 均为 true）→ /goal 自动结束

生成 /goal 条件（手动场景）：
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/fastship/orchestrator.py" goal
```

## 常用命令

```bash
FASTSHIP="python3 ${CLAUDE_PLUGIN_ROOT}/skills/fastship/orchestrator.py"
$FASTSHIP start "<需求>"   # 启动
$FASTSHIP start --session <id> "<需求>"  # 指定需求/feature 维度
$FASTSHIP start --base staging "<需求>"  # 指定 worktree base（默认 staging）
$FASTSHIP start --require-worktree "<需求>"  # base/worktree 不可用时硬失败
$FASTSHIP start --no-worktree "<需求>"  # 显式在当前工作区启动
$FASTSHIP next             # 当前步骤
$FASTSHIP done [--flags]   # 完成 + 验证
$FASTSHIP status           # 全部状态
$FASTSHIP list             # 列出全部需求 sessions
$FASTSHIP use <id>         # 切换 hook/CLI 默认 session
$FASTSHIP goal             # 生成 /goal 条件（Phase 2+ 可用）
$FASTSHIP adopt-branch     # 将活跃 session 迁移到当前分支
$FASTSHIP sweep-worktrees [--dry-run]  # 清理 fastship 创建且已完成+干净+合入 base 的 worktree
$FASTSHIP reset            # 重置当前 session
$FASTSHIP reset --all      # 清空全部 sessions
$FASTSHIP render-plan [plan.md]   # 把 plan 渲染成可视化 plan.html（缺省用当前 session 的 plan）
```

## Plan 可视化（plan.html）

Step 1.4 1B 技术方案通过校验后，orchestrator 自动在 plan.md 同目录生成同名 `*.plan.html`（hook + CLI 两种模式都会触发）：

- **生成后自动在浏览器打开**（直接给你看，无需手动触发）。
  - 自动打开开关 `FASTSHIP_PLAN_HTML_OPEN`：`auto`（缺省，非 CI/headless 才开）/ `always` / `never`。
- 图示渲染：`## 图示` 里 ```mermaid 流程图走 **ELK 布局**（比默认 dagre 少交叉、更清晰）；```dot/```graphviz 架构/依赖图走 **Graphviz-WASM**（层级布局最清楚）。都离线优雅降级（加载失败显示图源）。
- 离线自包含单文件，把 Goal/Architecture、**E2E↔AC 覆盖矩阵**、**模块架构图**（从 `## File Structure` 表派生）、正文渲染成直观视图，解决「纯 md 看不清」。正文/矩阵/模块图纯 Python 渲染完全离线；mermaid 走 CDN，离线降级显示图源。
- 按需重渲染：`render-plan [plan.md]`（缺省取当前 session 的 plan_path；不想弹浏览器用 `plan_html.py <plan> --no-open`）。
- 生成失败**不阻断**流程；plan.html **不进可信账本**（派生视图，非门禁交付物）；产物已 gitignore（`docs/superpowers/plans/*.plan.html`）。

## Phase 3：AC 驱动旅程验证（替代「数 turn」E2E）

验证单元是 **AC，不是 turn**。每条 Phase 1 锁定的 P0/P1 AC（`{id, assertion}`）一路串到底：

- **3.2 验证意图**：从锁定 AC + 实现 diff 派生 `verification-plan.json`，每条 AC 一个 intent（`ac_id` / `assertion` / `required_surfaces`（按 diff 落在哪个 app_paths 推导）/ `entry` / `goal` / `success_evidence` / 语义 `hints_from_diff`（role/text/label/testid，禁脆 CSS）/ config-gated AC 必填 `differential{flag,on_state,off_state}`）。
- **3.3 验证执行 · 看一眼再点**：浏览器 agent（默认 `agent-browser`）读真实 a11y 树（`snapshot`）→ 看到真实元素再点 → 关键状态截图 → 采 network/DOM 事实 → 记录**真实走过的** `realized_journey`（每步带 `target` 表面）。每条 AC 一个 `<ac_id>.bundle.json` + `evidence-manifest.json`（每 artifact 的 sha256）。cross-端 AC 旅程跨 admin/user/api；differential AC 走 ON→验→OFF→验→teardown 两态。
- **3.4 AC 裁判 + 结构 gate + 报告**：① 独立**对抗裁判**（≠实现者，真看截图）逐 AC 出 `verify-judge.json`（verdict + 引用真实 artifact + reason）；② `verify_gate.py` 跑**确定性结构 gate**（六道：AC 覆盖/surface 覆盖/differential/证据真实(sha256)/裁判引用有效/派生终判）；③ 永远生成 `*.verify-report.html`（路径+截图+逐 AC 裁判）。exit 0=PASS / 3=SURFACE(证据弱，看报告后 `verify_confirm` 放行) / 1=FAIL。

cross-端覆盖**靠结构强制**：`required_surfaces` 从 diff 派生（改了哪个 app_paths 就必须验哪个表面），`differential` 要求 ON/OFF 两态对照（只证共存不证因果 = FAIL）。`min_turns` 已废，不参与任何判定。UI feature 不再需要 N/A 死墙。

## 项目级验证配置

项目须把本地启动方式、端口、各表面 app_paths 写入 `.claude/fastship.project.json` 的 `verify` 段。fastship 的 `next` 指令、hook hash 记录、结构 gate 子进程与 HTML 报告都读同一份配置；禁止只写在 CLAUDE.md/README 里。

```json
{
  "verify": {
    "driver": "agent-browser",
    "result_dir": ".claude/fastship-verify",
    "surfaces": {
      "user":  { "base_url": "http://localhost:15173", "app_paths": ["apps/web-app"] },
      "admin": { "base_url": "http://localhost:15174", "app_paths": ["apps/admin-web"] },
      "api":   { "base_url": "http://localhost:3100",  "app_paths": ["services/api-server"] }
    },
    "setup_commands": ["./dev_local.sh", "cd apps/web-app && npm run dev", "cd apps/admin-web && npm run dev"]
  }
}
```

`surfaces.*.app_paths` 是 **required-surface 派生的依据**（diff 命中哪个 app_paths → 该表面成为必需）。旧 `e2e` 段在迁移期保留（`http` 驱动可读其 `setup_commands`），但判定走 `verify`。

## 核心红线

- Plan 必须走 writing-plans skill（orchestrator 验证 plan 文件签名，手写 plan 被拒）
- 1A 需求拷打 (1.3r，仅 feature)：多角色法庭（产品/运营/数据/财务，缺席须显式 abstain 到场）→ 书记员机械合成 → grill。引擎硬验**合成纪律**：additive 并集不减（书记员只搬运、不改写/不凭空造/不冒名来源）、exclusive fork 全 resolved、每 P0 有 source + ≥1 可观察 AC（`{id, assertion}`，AC id 全局唯一含 P1）、concern 必带 evidence_ref。verdict 派生自结构，自报 PASS 无效。
- 1B 技术方案 (1.4)：须为【每条 1A P0/P1 AC】显式映射 ≥1 task + ≥1 E2E（plan 内嵌 `ac_mapping` JSON 契约）；dangling/重复 ac_id/空 task|e2e/未全覆盖 = 当场 FAIL（不等 codex）。bugfix 无 1A，只验 writing-plans 签名。被 config/toggle 门控的 AC 若声明 `differential`，必须写全 `{flag,on_state,off_state}` + 非空 `required_surfaces`（cross-端覆盖契约，§6.4）。
- Grill 必须走 grill-me skill（orchestrator 验证 grill 摘要文件 ≥300B + 结构）；若 1B 声明了 open 技术 fork，grill 摘要须含 `fork_resolutions` 逐条回写非空 resolution（从**可信 plan** 复核 open fork 集，漏裁/空裁/裁非 open fork 即 FAIL）
- Codex Review 必须执行同一套 P0 contract / AC / E2E 证据审查，且输出机器可验证 JSON gate；纯文本 `GATE: PASS` 无效
- Codex Review FAIL 按**缺陷层**回退（F7）：`p0_requirements_missing` 非空（需求层：1A 漏/错订了某 P0）→ 回退 1.3r 重订需求；其余覆盖/质量问题（方案层）→ 回退 1.4 改 plan。hook 与 CLI 模式行为一致
- Code Review (2.5) 硬 gate — 对实现做对抗性 review，产出 `.claude/.fastship-code-review.md` 结构化 JSON gate；design_deviations/spec_gaps/quality_issues 任一非空即 FAIL；reviewed_against 须指向真实设计稿/spec，reviewed_files 须与 git diff 相交（防橡皮图章）
- 每个 step 产出的报告/artifact 必须写入 trusted artifact ledger（path + sha256 + size + step_id）；validator 必须重算 hash，记录后被改即 FAIL
- 执行必须走 executing-plans / subagent-driven-development
- 关键步骤禁止 fallback：没有当前 step artifact 记录或 gate state → validator 必须返回 false
- 主线程禁止亲自 grep/find（改为 1.2 并行 Explore）
- 一 session 一 worktree：`start` 默认从 `origin/staging` 创建 `.claude/worktrees/<session>` 隔离 worktree；并行需求放各自 git worktree。同 state-home 内 start 第二个活跃 session 默认被拒，须 `--session` 显式开新 session；`--shared` 只复用当前 worktree。多活跃 session 时 hook 停止自动推进以防串台。
- Worktree cleanup 只清 fastship 自己创建、状态为 done/stopped、工作区干净、HEAD 已并入 base ref 的 worktree；当前/main/dirty/unmerged/外部 worktree 一律保留。删除不用 `--force`，分支删除用 `git branch -d`。
- Phase 2 implement 扇出读【计划树骨架】决定（🌳 driver 不读全 plan）：1.4 通过后引擎把 plan 拆成 root.md / nodes/<id>.md / briefs/<id>.md / skeleton.json（artifacts.plan_tree）。driver 从 skeleton.json 读 nodes 的 id/deps/files 做拓扑扇出——files 不相交且无依赖边的 node 才在同 worktree 内并行编辑（不各自 commit，主线程逐组 commit），相交/依赖的串行；每个 implement subagent 只收一条 briefs/<id>.md（root + 本 node 正文 + 上游 output 契约），driver 绝不把 node 正文读进自己 context（否则逐片累积又回满 plan）。运行期每 node 完成捕获 git diff 校验 files_changed ⊆ node.files（越界=node FAIL），driver 是 skeleton.json 唯一 writer。门禁强度：instruction 级 + 预拼 brief 结构兜底 + git diff 运行期复核（Phase 2 无 hook，非 hook 级硬门禁）。禁止并行跑测试套件/E2E（只编辑+编译检查）。
- Implement verdict 落 session 绑定的 ledger（sessions/<sid>/implement-verdicts.md），喂 Step 2.5。
- Phase 1 编辑代码文件 → hook 自动 BLOCK + 打印当前步骤（Claude Code only）
- 验证阶段禁止 bash 裸 SQL DB 写入（Gate 0 拦截）；但 cross-端验证经真实 admin UI 写 config 不拦（feature 自身预期写入路径，§6.5），旅程末尾须 teardown 恢复
- Loop 上限 3 次（gate 锁死）
- KNOWLEDGE.md merge 前必须表态（gate 拦截）
- 禁止自我豁免验证步骤：验证步骤做不了 → 暂停 + 报告阻碍原因，等用户决策。禁止以"无法自动化"、"该 feature 特殊"、"没有 mock endpoint"、"时间紧"为由自行降级、替代或跳过任何步骤。豁免权归用户，不归 Claude。

## P0 Contract / AC / E2E 硬约束

Phase 1 的 AC 和 E2E 不能只是 plan 文本里的声明，必须形成可审查的覆盖合同：

- Step 报告/artifact 不允许自证。Brief、Plan、Grill、Codex Review、E2E 报告、KNOWLEDGE 更新都必须绑定当前 step provenance/hash。
- P0/P1 需求必须来自用户原始需求或 Context Brief 证据，不能由 agent 自行降级、改写或删除核心目标。
- 每个 P0/P1 需求必须有可观察 AC；每个 AC 必须映射到至少一个 E2E scenario。
- E2E 主断言必须验证业务结果或可观察证据，例如 URL 跳转、network 请求、API 响应、DOM 状态变化、持久化结果或截图证据。
- `button visible`、`page loads`、`status 200`、`no console error`、`text contains` 只能作为 smoke/辅助断言，不能算核心需求覆盖。
- Phase 3 只能运行 Phase 1 确认的场景；发现未覆盖 P0/P1 AC、弱 scenario、缺 evidence trace 时必须 FAIL。

Codex Review 必须同步遵循上述规则。`.claude/.fastship-codex-review.md` 必须包含结构化 JSON gate：

```json
{
  "gate": "PASS",
  "reviewed_plan_sha256": "<当前 1.4 plan artifact sha256>",
  "p0_contract_reviewed": true,
  "ac_e2e_coverage_reviewed": true,
  "weak_case_reviewed": true,
  "evidence_plan_reviewed": true,
  "p0_requirements_missing": [],
  "uncovered_ac": [],
  "unmapped_e2e_scenarios": [],
  "weak_scenarios": [],
  "non_business_assertions": [],
  "missing_evidence": []
}
```

`reviewed_plan_sha256` 必须等于当前 1.4 plan artifact hash。任一审查布尔项不是 `true`，或任一问题数组非空，Codex Review 必须 `FAIL`，orchestrator 不得推进到用户确认。FAIL 时 orchestrator 按缺陷层回退（F7）：`p0_requirements_missing` 非空 → 回退 1.3r（需求层，1A 重订需求，光改 plan 修不了缺的需求）；其余数组非空 → 回退 1.4（方案层）。bugfix 无 1A，一律回 1.4；解析不出 gate 时 fail-closed 回 1.4。

Code Review (Step 2.5) 复用同一结构化 gate 合同，但审查对象是**实现代码**而非 plan。`.claude/.fastship-code-review.md` 必须包含结构化 JSON gate：

```json
{
  "gate": "PASS",
  "reviewed_against": "<真实设计稿/spec 文件路径，须存在>",
  "reviewed_files": ["<本次实现改动的真实文件，须存在>"],
  "design_fidelity_reviewed": true,
  "spec_compliance_reviewed": true,
  "quality_reviewed": true,
  "design_deviations": [],
  "spec_gaps": [],
  "quality_issues": [],
  "unverified_claims": []
}
```

`reviewed_against` 须指向真实存在的设计稿/spec 源文件；`reviewed_files` 须为真实存在文件列表，且当 git diff 可观察时其 basename 必须与改动文件相交（防橡皮图章）。任一审查布尔项不是 `true`，或任一问题数组（design_deviations/spec_gaps/quality_issues/unverified_claims）非空，Code Review 必须 `FAIL` → 留在 2.5 修复实现后重 review（不回退 plan）。

验证证据 manifest（`evidence-manifest.json`）的 sha256 记入 gate state（`verify_evidence_hash`，与 `e2e_result_hash` 同源），每个截图/快照 artifact 也按 manifest 记 sha256。manifest 或 artifact 在执行后被改 → Step 3.3/3.4 结构 gate 必须 `FAIL`（证据真实检查）。裁判 `verify-judge.json` 每条 verdict 必须引用真实存在、属于该 AC 的 artifact，伪造引用 = 结构 gate FAIL（反橡皮图章）。

## 状态行

每次回复末尾包含：

```
🚀 /fastship | {需求简述} | Step: {当前步骤 id} | Phase: {1/2/3} | Loop: {0/3}
```

status 命令额外输出机器可读行（供 /goal 评估器解析）：

```
[FASTSHIP_GOAL] step=3.2 phase=3 test_passed=true e2e_executed=false e2e_gate_passed=false code_reviewed=false knowledge_acknowledged=false loop=0/3
```

🔴 /goal 模式下，每完成关键步骤后务必运行 status 命令，让评估器看到最新进度。
