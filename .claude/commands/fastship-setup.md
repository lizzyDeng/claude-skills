---
description: 在当前项目中安装 /fastship skill 的完整工具链（skill 定义 + hooks + E2E 工具）
---

你需要在当前项目中安装 /fastship skill 的完整工具链。按以下步骤执行：

## Step 0: 检测前置依赖（🔴 必需）

### 0.1 superpowers 插件

fastship 的阶段 1.4 / 阶段 2 强制调用 superpowers 的 skill（`writing-plans` / `executing-plans`）。

1. 检查插件是否已装：`ls ~/.claude/plugins/cache/claude-plugins-official/superpowers/*/skills/ 2>/dev/null`
2. 找得到 `writing-plans` 和 `executing-plans` 目录 → ✅ 继续
3. 找不到 → ⚠️ 告诉用户：

   ```
   未检测到 superpowers 插件。fastship 的 Plan Gate 会在你第一次编辑代码时卡住，
   因为 writing-plans / executing-plans skill 不可用。

   请先在 Claude Code 里安装：
     /plugin install superpowers@claude-plugins-official

   装完后重新执行 /fastship-setup。
   ```

   问用户是否继续（用户明确表示"继续"才往下走，否则停在这一步）。

### 0.2 全局 grill-me skill

fastship 的阶段 1.5 强制调用全局 `grill-me` 对 plan 做结构化拷问。

1. 检查 skill 是否可用：`ls ~/.claude/skills/grill-me 2>/dev/null || find ~/.claude -maxdepth 5 -type d -name "grill-me" 2>/dev/null | head -3`
2. 找得到 → ✅ 继续
3. 找不到 → ⚠️ 告诉用户：

   ```
   未检测到全局 grill-me skill。fastship 的阶段 1.5 会卡住，
   因为 Plan Grilling 关卡需要它来对 plan 做结构化拷问。

   请把 grill-me skill 安装到 ~/.claude/skills/grill-me/，
   或确保你常用的 skills 集合里包含它（在当前会话能 Skill(skill="grill-me") 调用即可）。
   ```

   问用户是否继续（用户明确表示"继续"才往下走，否则停在这一步）。

## Step 1: 检测项目

1. 确认当前目录是一个 git 仓库（`git rev-parse --show-toplevel`）
2. 检测项目技术栈（查看 Cargo.toml / package.json / pyproject.toml / go.mod 等）
3. 输出检测结果告知用户

## Step 2: 安装 skill 定义

1. 创建 `.claude/commands/` 目录
2. 将 `/Users/apple/works/claude-skills/skills/fastship/SKILL.md` 复制到 `.claude/commands/fastship.md`

## Step 3: 安装 hooks + orchestrator

1. 创建 `.claude/hooks/` 目录
2. 将 `/Users/apple/works/claude-skills/skills/fastship/hooks/ship_verify_gate.py` 复制到 `.claude/hooks/ship_verify_gate.py`
3. 创建 `.claude/tools/` 目录
4. 将 `/Users/apple/works/claude-skills/skills/fastship/orchestrator.py` 复制到 `.claude/tools/fastship_orchestrator.py`
5. 将 `/Users/apple/works/claude-skills/skills/fastship/fastship_state.py` 复制到 `.claude/tools/fastship_state.py`
6. 将 `/Users/apple/works/claude-skills/skills/fastship/fastship` 复制到 `.claude/tools/fastship`，并执行 `chmod +x .claude/tools/fastship`

## Step 4: 安装 E2E 工具

1. 创建 `tests/` 和 `tests/e2e_scenarios/` 目录
2. 将 `/Users/apple/works/claude-skills/skills/fastship/e2e/e2e_runner.py` 复制到 `tests/e2e_runner.py`
3. 将 `/Users/apple/works/claude-skills/skills/fastship/e2e/e2e_gate.py` 复制到 `tests/e2e_gate.py`
4. 将 `/Users/apple/works/claude-skills/skills/fastship/e2e/scenario_template.json` 复制到 `tests/e2e_scenarios/_template.json`

## Step 5: 配置 hooks

hooks 指向 orchestrator（orchestrator 内部 subprocess 调用 ship_verify_gate）。
先保存项目根目录：`PROJECT_ROOT="$(git rev-parse --show-toplevel)"`。
下面 JSON 里的 `<PROJECT_ROOT>` 必须替换成 `$PROJECT_ROOT` 的绝对路径；不要使用相对 `.claude/tools/...`，否则 agent `cd` 到子目录或其他目录后 hook 会找不到脚本。

读取当前项目的 `.claude/settings.local.json`（如果存在），将以下 hooks 配置**合并**进去（不覆盖已有配置）：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 <PROJECT_ROOT>/.claude/tools/fastship_orchestrator.py pre_edit",
            "timeout": 10,
            "statusMessage": "Orchestrator: phase check + plan gate..."
          }
        ]
      },
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 <PROJECT_ROOT>/.claude/tools/fastship_orchestrator.py pre_edit",
            "timeout": 10,
            "statusMessage": "Orchestrator: phase check + plan gate..."
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 <PROJECT_ROOT>/.claude/tools/fastship_orchestrator.py pre_bash",
            "timeout": 10,
            "statusMessage": "Orchestrator: gates 0-5..."
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 <PROJECT_ROOT>/.claude/tools/fastship_orchestrator.py post_bash",
            "timeout": 10,
            "statusMessage": "Orchestrator: auto-detect step completion..."
          }
        ]
      },
      {
        "matcher": "Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 <PROJECT_ROOT>/.claude/tools/fastship_orchestrator.py post_edit",
            "timeout": 10,
            "statusMessage": "Orchestrator: auto-detect file writes..."
          }
        ]
      },
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 <PROJECT_ROOT>/.claude/tools/fastship_orchestrator.py post_edit",
            "timeout": 10,
            "statusMessage": "Orchestrator: auto-detect file writes..."
          }
        ]
      }
    ]
  }
}
```

**合并规则**：
- 如果 `.claude/settings.local.json` 不存在，直接创建
- 如果存在，读取现有内容，将 hooks 配置合并到 `hooks.PostToolUse` 和 `hooks.PreToolUse` 数组中
- 不要重复添加（检查 command 字段是否已包含 `fastship_orchestrator.py`）
- 保留所有已有的 permissions 和其他配置

## Step 6: 更新 .gitignore

在项目 `.gitignore` 中追加（如果不存在）：

```
.claude/.ship-verify-state.json
.claude/.fastship-orchestrator-state.json
.claude/state/
.claude/.fastship-brief.md
.claude/.fastship-grill-result.md
```

## Step 7: 验证

```bash
# Hook gate 状态
python3 "$(git rev-parse --show-toplevel)/.claude/hooks/ship_verify_gate.py" status

# Orchestrator 状态（应显示"没有活跃 session"）
.claude/tools/fastship status
```

hook gate 输出应包含 `Recall / Plan / Test / E2E / Knowledge / Loop` 六行状态。
orchestrator 输出应为 "❌ 没有活跃 session。"（正常，start 后才有）。

## Step 8: 输出总结

告诉用户：

1. 检测到的技术栈
2. 安装了哪些文件（包括 `.claude/commands/fastship.md` skill 定义）
3. 配置了哪些 hooks（6 个 Gate 自动生效）：
   - **Gate B (pre_edit)** — 编辑代码前必须有 plan + 已 `knowledge_recall`
   - **Gate 0-3 (pre_bash)** — DB 写入拦截 / E2E 前置 / E2E Gate 前置 / merge-push 前置
   - **Gate 4 (pre_bash)** — merge/push 必须 KNOWLEDGE.md 已表态
   - **Gate 5 (pre_bash)** — 重跑 E2E 必须先 loop_record；loop_count==3 锁死
4. **Orchestrator 是 hook 入口**：每次 Edit/Write/Bash 自动触发 orchestrator，orchestrator 内部委托 ship_verify_gate
5. orchestrator CLI 命令：
   - `.claude/tools/fastship start "<需求>"` — 启动 session
   - `.claude/tools/fastship next` — 查看当前步骤
   - `.claude/tools/fastship done [--flags]` — 完成步骤 + 验证
   - `.claude/tools/fastship status` — 查看全部步骤状态
   - `.claude/tools/fastship adopt-branch` — 将活跃 session 迁移到当前分支
   - `.claude/tools/fastship reset` — 重置（同时清除 hook state）
6. 提醒：`/fastship` 即可开始使用，orchestrator 驱动全流程，16 步中 12 步自动推进
7. 提醒：`e2e_runner.py` 是通用模板，如项目有特殊需求可按需定制
8. 提醒：Codex / 其他 agent 也可用（CLI 模式，15 步全部手动 done，但仍有 artifact 硬验证）
