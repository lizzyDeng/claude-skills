---
name: fastship
description: "Result-driven development skill. Python orchestrator drives every step with hard validation. Works in Claude Code (hook mode) and Codex/other agents (CLI mode)."
---

# /fastship — 结果驱动开发（Python 编排版）

E2E 验证通过为唯一交付标准。Python 状态机驱动每一步，artifact 硬验证，不能跳步。

## 启动

🧠 **Context 预检**（机械强制）：`start` 命令会检查最近 2 分钟内是否执行过 `/compact`。未 compact → BLOCK。先 `/compact` 再 start。

收到需求后立即运行：
  "$(git rev-parse --show-toplevel)/.claude/tools/fastship" start "<需求>"

fastship 支持并行多个活跃需求。每个需求有独立 session/state：
`{git-dir}/fastship/sessions/<session-id>/orchestrator.json` + `gate.json`。
默认 session 由需求文本生成；需要和 Forge feature 对齐时使用 `--session <feature-slug>`。

## 双模工作方式

### Claude Code（hook 模式 — 最强）

orchestrator 是 hook 入口。每次 Edit/Write/Bash 自动触发：
- **pre_edit**: Phase 1 阻止编辑代码，打印当前步骤
- **post_edit/post_bash**: 自动检测步骤完成，推进下一步

17 步中多数步骤由 hook 自动推进，少数确认/决策步骤需手动：
  "$(git rev-parse --show-toplevel)/.claude/tools/fastship" done [--flags]

### Codex / 其他 Agent（CLI 模式）

无 hook，agent 手动驱动每一步：
  1. `"$(git rev-parse --show-toplevel)/.claude/tools/fastship" next` → 读当前步骤指令
  2. 执行步骤
  3. `"$(git rev-parse --show-toplevel)/.claude/tools/fastship" done [--flags]` → 验证 + 推进
  4. 重复

全部 17 步需手动 done，但 protected validators 不允许 filesystem fallback。
无 hook/gate state 的关键步骤（plan provenance、Codex review、E2E、report、gate、loop pass、knowledge）必须失败，不能靠文件存在自动通过。
Codex/CLI 模式下，文件产物步骤必须显式绑定 artifact：`done --brief <path>`、`done --plan <path>`、`done --grill <path>`、`done --codex-review <path>`、`done --report <path>`、`done --knowledge <path>`。没有绑定就不能通过。

## 流程概览

```
Phase 1: Brainstorm (8 步)
  1.0  需求分类         [CC:auto | Codex:done] classify CLI
  1.1  上下文+recall    [CC:auto | Codex:done] knowledge_recall CLI
  1.2  并行 Explore     [CC:done  | Codex:done] done --agents N (≥3)
  1.3  Context Brief    [CC:auto | Codex:done] .fastship-brief.md 验证章节
  1.3d Bug 诊断         [CC:auto | Codex:done] fix_verified (仅 bugfix)
  1.4  写计划           [CC:auto | Codex:done] plan 文件 + writing-plans 签名
  1.5  Grill            [CC:auto | Codex:done] .fastship-grill-result.md 验证
  1.5c Codex Review     [CC:auto | Codex:done] .fastship-codex-review.md PASS/FAIL
                        FAIL → 回退 1.4 更新 plan → 重走 1.5 → 1.5c
  1.6  用户确认         [CC:done  | Codex:done] done --user-confirmed

Phase 2+3: /goal 自主执行（Plan 确认后自动触发）
  2.0  执行计划         [/goal 自主驱动]
  3.0  冒烟测试         [/goal 自主驱动]
  3.1  项目测试         [/goal + hook auto-detect]
  3.2  E2E Runner       [/goal + hook auto-detect]
  3.3  E2E 报告         [/goal + hook auto-detect]
  3.4  E2E Gate         [/goal + hook auto-detect]
  3.5  Loop Record      [/goal 自主决策: fail→auto continue, 3次后暂停]
  3.6  KNOWLEDGE 闭环   [/goal + hook auto-detect]
```

## /goal 自主执行（Phase 2+3）

Plan 确认后（步骤 1.6 完成），orchestrator 自动输出 `/goal` 命令。用户执行后：

- Claude 自主驱动 Phase 2（执行）+ Phase 3（验证）全流程
- Haiku 评估器通过 `[FASTSHIP_GOAL]` 状态行判断是否完成
- 每步完成后 Claude 运行 `status`，评估器解析 `step=` / `test_passed=` / `e2e_executed=` 等字段
- Loop Record fail 时 Claude 自主选择 continue（在 3 次上限内），3 次后暂停等用户介入
- 全部完成（step=done + 三个 gate flag 均为 true）→ /goal 自动结束

生成 /goal 条件（手动场景）：
```bash
"$(git rev-parse --show-toplevel)/.claude/tools/fastship" goal
```

## 常用命令

```bash
FASTSHIP="$(git rev-parse --show-toplevel)/.claude/tools/fastship"
"$FASTSHIP" start "<需求>"   # 启动
"$FASTSHIP" start --session <id> "<需求>"  # 指定需求/feature 维度
"$FASTSHIP" next             # 当前步骤
"$FASTSHIP" done [--flags]   # 完成 + 验证
"$FASTSHIP" status           # 全部状态
"$FASTSHIP" list             # 列出全部需求 sessions
"$FASTSHIP" use <id>         # 切换 hook/CLI 默认 session
"$FASTSHIP" goal             # 生成 /goal 条件（Phase 2+ 可用）
"$FASTSHIP" adopt-branch     # 将活跃 session 迁移到当前分支
"$FASTSHIP" reset            # 重置当前 session
"$FASTSHIP" reset --all      # 清空全部 sessions
```

## 项目级 E2E 配置

项目如需固定本地启动方式、端口、scenario 或 runner 参数，必须写入 `.claude/fastship.project.json`。fastship 的 `next` 指令、hook hash 记录、E2E 报告校验和 Gate 子进程都会读取同一份配置；禁止只把启动命令写在 CLAUDE.md/README 里。

示例：

```json
{
  "e2e": {
    "setup_commands": ["./dev_local.sh"],
    "runner_command": "python3 tests/e2e_runner.py --base-url http://localhost:3100 --health /health --scenario tests/e2e_scenarios/core.json -o /tmp/e2e_result.json",
    "gate_command": "python3 tests/e2e_gate.py --result /tmp/e2e_result.json --min-turns 10",
    "result_path": "/tmp/e2e_result.json",
    "min_turns": 10,
    "notes": ["Use the project local dev script so E2E runs against the same services as development."]
  }
}
```

`runner_command`/`gate_command` 是给 agent 执行的标准指令；`result_path`/`min_turns` 是 validator 的硬输入。两边必须一致，否则 Step 3.3/3.4 会因为 hash、turns 或 Gate 参数不一致而失败。

## 核心红线

- Plan 必须走 writing-plans skill（orchestrator 验证 plan 文件签名，手写 plan 被拒）
- Grill 必须走 grill-me skill（orchestrator 验证 grill 摘要文件 ≥300B + 结构）
- Codex Review 必须执行同一套 P0 contract / AC / E2E 证据审查，且输出机器可验证 JSON gate；纯文本 `GATE: PASS` 无效
- 每个 step 产出的报告/artifact 必须写入 trusted artifact ledger（path + sha256 + size + step_id）；validator 必须重算 hash，记录后被改即 FAIL
- 执行必须走 executing-plans / subagent-driven-development
- 关键步骤禁止 fallback：没有当前 step artifact 记录或 gate state → validator 必须返回 false
- 主线程禁止亲自 grep/find（改为 1.2 并行 Explore）
- Phase 1 编辑代码文件 → hook 自动 BLOCK + 打印当前步骤（Claude Code only）
- E2E 阶段禁止 DB 写入（gate 拦截）
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

`reviewed_plan_sha256` 必须等于当前 1.4 plan artifact hash。任一审查布尔项不是 `true`，或任一问题数组非空，Codex Review 必须 `FAIL`，orchestrator 不得推进到用户确认。

E2E 报告必须引用 gate state 中的 `e2e_result_hash`。报告没有绑定 runner 原始结果 hash，或报告文件在记录后被修改，Step 3.3 必须 `FAIL`。

## 状态行

每次回复末尾包含：

```
🚀 /fastship | {需求简述} | Step: {当前步骤 id} | Phase: {1/2/3} | Loop: {0/3}
```

status 命令额外输出机器可读行（供 /goal 评估器解析）：

```
[FASTSHIP_GOAL] step=3.2 phase=3 test_passed=true e2e_executed=false knowledge_acknowledged=false loop=0/3
```

🔴 /goal 模式下，每完成关键步骤后务必运行 status 命令，让评估器看到最新进度。
