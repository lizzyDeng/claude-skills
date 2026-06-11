---
name: fastship
description: "Result-driven development skill. Python orchestrator drives every step with hard validation. Works in Claude Code (hook mode) and Codex/other agents (CLI mode)."
---

# /fastship — 结果驱动开发（Python 编排版）

E2E 验证通过为唯一交付标准。Python 状态机驱动每一步，artifact 硬验证，不能跳步。

> 引擎路径用 `${CLAUDE_PLUGIN_ROOT}`（插件安装后由 Claude Code 注入）。源/dev 调试（非插件模式）时直接运行源树 `skills/fastship/orchestrator.py`。

## 启动

收到需求后立即运行：
  python3 "${CLAUDE_PLUGIN_ROOT}/skills/fastship/orchestrator.py" start "<需求>"

默认 `start` 会尽力从 `origin/staging` 创建隔离 worktree：
先同步 `staging`（checked out 时 `git pull --ff-only origin staging`，否则 `git fetch origin staging`），
再 `git worktree add -b fastship/<session> .claude/worktrees/<session> origin/staging`。
创建成功后必须 `cd` 到输出的 worktree 路径继续执行；创建失败且未加 `--require-worktree` 时降级为当前工作区启动并打印警告。

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

## 双模工作方式

### Claude Code（hook 模式 — 最强）

orchestrator 是 hook 入口。每次 Edit/Write/Bash 自动触发：
- **pre_edit**: Phase 1 阻止编辑代码，打印当前步骤
- **post_edit/post_bash**: 自动检测步骤完成，推进下一步

19 步中多数自动推进，关键关卡需手动：
  python3 "${CLAUDE_PLUGIN_ROOT}/skills/fastship/orchestrator.py" done [--flags]

### Codex / 其他 Agent（CLI 模式）

无 hook，agent 手动驱动每一步：
  1. `python3 "${CLAUDE_PLUGIN_ROOT}/skills/fastship/orchestrator.py" next` → 读当前步骤指令
  2. 执行步骤
  3. `python3 "${CLAUDE_PLUGIN_ROOT}/skills/fastship/orchestrator.py" done [--flags]` → 验证 + 推进
  4. 重复

全部 19 步需手动 done，但 done 仍做硬性 artifact 验证（文件存在、内容检查）。
Validators 自动检测环境：有 hook state 用 hook state，没有则直接扫文件系统。

## 流程概览

```
Phase 1: Brainstorm (10 步)
  1.0  需求分类         [CC:auto | Codex:done] classify CLI
  1.1  上下文+recall    [CC:auto | Codex:done] knowledge_recall CLI
  1.2  并行 Explore     [CC:done  | Codex:done] done --agents N (≥3)
  1.3  Context Brief    [CC:auto | Codex:done] .fastship-brief.md 验证章节
  1.3r 1A 需求拷打      [CC:auto | Codex:done] 多角色法庭→书记员合成→grill→需求定稿 .fastship-requirements.md (仅 feature；bugfix 跳)
  1.3d Bug 诊断         [CC:auto | Codex:done] fix_verified (仅 bugfix)
  1.4  1B 技术方案      [CC:auto | Codex:done] RD/QA/总结者法庭→writing-plans 签名 + 每条 1A P0/P1 AC→task+E2E 映射 (bugfix 只验签名)
  1.5  Grill            [CC:auto | Codex:done] 裁决 1B open 技术 fork（须 fork_resolutions 回写 resolution）；feature 无 open fork 自动跳过(F4)，bugfix 照跑
  1.5c Codex Review     [CC:done  | Codex:done] .fastship-codex-review.md GATE:PASS
  1.6  用户确认         [CC:done  | Codex:done] done --user-confirmed

Phase 2: Execution (2 步)
  2.0  执行计划         [CC:done  | Codex:done]
  2.5  Code Review      [CC:done  | Codex:done] .fastship-code-review.md gate

Phase 3: Verification (7 步)
  3.0  冒烟测试         [CC:done  | Codex:done]
  3.1  项目测试         [CC:auto | Codex:done] test pass
  3.2  E2E Runner       [CC:auto | Codex:done] .claude/fastship-e2e-result.json
  3.3  E2E 报告         [CC:auto | Codex:done] 报告文件 ≥200B
  3.4  E2E Gate         [CC:auto | Codex:done] e2e_gate
  3.5  Loop Record      [CC:半auto | Codex:done --outcome pass|fail] fail→手动 --decision
  3.6  KNOWLEDGE 闭环   [CC:auto | Codex:done] KNOWLEDGE.md
```

## 常用命令

```bash
FASTSHIP="python3 ${CLAUDE_PLUGIN_ROOT}/skills/fastship/orchestrator.py"
$FASTSHIP start "<需求>"   # 启动
$FASTSHIP start --base staging "<需求>"  # 指定 worktree base（默认 staging）
$FASTSHIP start --require-worktree "<需求>"  # base/worktree 不可用时硬失败
$FASTSHIP start --no-worktree "<需求>"  # 显式在当前工作区启动
$FASTSHIP next             # 当前步骤
$FASTSHIP done [--flags]   # 完成 + 验证
$FASTSHIP status           # 全部状态
$FASTSHIP adopt-branch     # 将活跃 session 迁移到当前分支
$FASTSHIP sweep-worktrees [--dry-run]  # 清理 fastship 创建且已完成+干净+合入 base 的 worktree
$FASTSHIP reset            # 重置
```

## 核心红线

- Plan 必须走 writing-plans skill（orchestrator 验证 plan 文件签名，手写 plan 被拒）
- 1A 需求拷打 (1.3r，仅 feature)：多角色法庭→书记员合成→grill；硬验合成纪律（并集不减/不改写/不冒名）、fork 全 resolved、每 P0 有 source+≥1 AC（{id,assertion}，id 全局唯一含 P1）
- 1B 技术方案 (1.4)：每条 1A P0/P1 AC 须映射 ≥1 task+≥1 E2E（ac_mapping JSON）；dangling/重复/空/未全覆盖 = 当场 FAIL
- Grill 必须走 grill-me skill（≥300B + 结构）；1B 有 open 技术 fork 时须含 fork_resolutions 逐条回写（从可信 plan 复核，漏裁/空裁 FAIL）
- Codex Review FAIL 按缺陷层回退（F7）：p0_requirements_missing→1.3r（需求层）/ 其余→1.4（方案层）；hook 与 CLI 一致
- 执行必须走 executing-plans / subagent-driven-development
- 主线程禁止亲自 grep/find（改为 1.2 并行 Explore）
- 一 session 一 worktree：`start` 默认从 `origin/staging` 创建 `.claude/worktrees/<session>` 隔离 worktree；`--shared` 只复用当前 worktree。cleanup 只清 fastship 自己创建、done/stopped、干净且已并入 base 的 worktree；当前/main/dirty/unmerged/外部 worktree 一律保留。
- Phase 1 编辑代码文件 → hook 自动 BLOCK + 打印当前步骤（Claude Code only）
- E2E 阶段禁止 DB 写入（gate 拦截）
- Loop 上限 3 次（gate 锁死）
- KNOWLEDGE.md merge 前必须表态（gate 拦截）

## 状态行

每次回复末尾包含：

```
🚀 /fastship | {需求简述} | Step: {当前步骤 id} | Phase: {1/2/3} | Loop: {0/3}
```
