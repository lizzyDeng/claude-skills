---
name: fastship
description: "Result-driven development skill. Python orchestrator drives every step with hard validation. Works in Claude Code (hook mode) and Codex/other agents (CLI mode)."
---

# /fastship — 结果驱动开发（Python 编排版）

E2E 验证通过为唯一交付标准。Python 状态机驱动每一步，artifact 硬验证，不能跳步。

## 启动

收到需求后立即运行：
  "$(git rev-parse --show-toplevel)/.claude/tools/fastship" start "<需求>"

## 双模工作方式

### Claude Code（hook 模式 — 最强）

orchestrator 是 hook 入口。每次 Edit/Write/Bash 自动触发：
- **pre_edit**: Phase 1 阻止编辑代码，打印当前步骤
- **post_edit/post_bash**: 自动检测步骤完成，推进下一步

16 步中 12 步自动推进，4 步需手动：
  "$(git rev-parse --show-toplevel)/.claude/tools/fastship" done [--flags]

### Codex / 其他 Agent（CLI 模式）

无 hook，agent 手动驱动每一步：
  1. `"$(git rev-parse --show-toplevel)/.claude/tools/fastship" next` → 读当前步骤指令
  2. 执行步骤
  3. `"$(git rev-parse --show-toplevel)/.claude/tools/fastship" done [--flags]` → 验证 + 推进
  4. 重复

全部 16 步需手动 done，但 done 仍做硬性 artifact 验证（文件存在、内容检查）。
Validators 自动检测环境：有 hook state 用 hook state，没有则直接扫文件系统。

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
"$FASTSHIP" next             # 当前步骤
"$FASTSHIP" done [--flags]   # 完成 + 验证
"$FASTSHIP" status           # 全部状态
"$FASTSHIP" goal             # 生成 /goal 条件（Phase 2+ 可用）
"$FASTSHIP" adopt-branch     # 将活跃 session 迁移到当前分支
"$FASTSHIP" reset            # 重置
```

## 核心红线

- Plan 必须走 writing-plans skill（orchestrator 验证 plan 文件签名，手写 plan 被拒）
- Grill 必须走 grill-me skill（orchestrator 验证 grill 摘要文件 ≥300B + 结构）
- 执行必须走 executing-plans / subagent-driven-development
- 主线程禁止亲自 grep/find（改为 1.2 并行 Explore）
- Phase 1 编辑代码文件 → hook 自动 BLOCK + 打印当前步骤（Claude Code only）
- E2E 阶段禁止 DB 写入（gate 拦截）
- Loop 上限 3 次（gate 锁死）
- KNOWLEDGE.md merge 前必须表态（gate 拦截）
- 禁止自我豁免验证步骤：验证步骤做不了 → 暂停 + 报告阻碍原因，等用户决策。禁止以"无法自动化"、"该 feature 特殊"、"没有 mock endpoint"、"时间紧"为由自行降级、替代或跳过任何步骤。豁免权归用户，不归 Claude。

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
