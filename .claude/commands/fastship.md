---
name: fastship
description: "Result-driven development skill. Python orchestrator drives every step with hard validation. Works in Claude Code (hook mode) and Codex/other agents (CLI mode)."
---

# /fastship — 结果驱动开发（Python 编排版）

E2E 验证通过为唯一交付标准。Python 状态机驱动每一步，artifact 硬验证，不能跳步。

## 启动

收到需求后立即运行：
  python3 .claude/tools/fastship_orchestrator.py start "<需求>"

## 双模工作方式

### Claude Code（hook 模式 — 最强）

orchestrator 是 hook 入口。每次 Edit/Write/Bash 自动触发：
- **pre_edit**: Phase 1 阻止编辑代码，打印当前步骤
- **post_edit/post_bash**: 自动检测步骤完成，推进下一步

16 步中 12 步自动推进，4 步需手动：
  python3 .claude/tools/fastship_orchestrator.py done [--flags]

### Codex / 其他 Agent（CLI 模式）

无 hook，agent 手动驱动每一步：
  1. `python3 .claude/tools/fastship_orchestrator.py next` → 读当前步骤指令
  2. 执行步骤
  3. `python3 .claude/tools/fastship_orchestrator.py done [--flags]` → 验证 + 推进
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

Phase 2: Execution (1 步)
  2.0  执行计划         [CC:done  | Codex:done]

Phase 3: Verification (7 步)
  3.0  冒烟测试         [CC:done  | Codex:done]
  3.1  项目测试         [CC:auto | Codex:done] test pass
  3.2  E2E Runner       [CC:auto | Codex:done] /tmp/e2e_result.json
  3.3  E2E 报告         [CC:auto | Codex:done] 报告文件 ≥200B
  3.4  E2E Gate         [CC:auto | Codex:done] e2e_gate
  3.5  Loop Record      [CC:半auto | Codex:done --outcome pass|fail] fail→手动 --decision
  3.6  KNOWLEDGE 闭环   [CC:auto | Codex:done] KNOWLEDGE.md
```

## 常用命令

```bash
python3 .claude/tools/fastship_orchestrator.py start "<需求>"  # 启动
python3 .claude/tools/fastship_orchestrator.py next            # 当前步骤
python3 .claude/tools/fastship_orchestrator.py done [--flags]  # 完成 + 验证
python3 .claude/tools/fastship_orchestrator.py status          # 全部状态
python3 .claude/tools/fastship_orchestrator.py reset           # 重置
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

## 状态行

每次回复末尾包含：

```
🚀 /fastship | {需求简述} | Step: {当前步骤 id} | Phase: {1/2/3} | Loop: {0/3}
```
