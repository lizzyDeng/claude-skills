# /forge 安装指南

推荐使用 `/forge-setup` 一键安装。以下是手动安装步骤。

## 0. 前置依赖

### 0.1 /fastship（🔴 必需）

forge 包裹 fastship，必须先安装 fastship。确认以下文件存在：

```bash
ls .claude/commands/fastship.md
ls .claude/hooks/ship_verify_gate.py
ls .claude/tools/fastship_orchestrator.py
```

未装 fastship → 运行 `/fastship-setup` 先安装。

### 0.2 superpowers 插件（🔴 必需）

forge 通过 fastship 间接依赖 superpowers（writing-plans, executing-plans）。

## 1. 复制 skill 定义

```bash
cp /path/to/claude-skills/skills/forge/SKILL.md .claude/commands/forge.md
```

## 2. 复制 gate 脚本

```bash
cp /path/to/claude-skills/skills/forge/hooks/forge_gate.py .claude/hooks/
```

## 3. 配置 hooks

在 `.claude/settings.local.json` 中**合并**以下配置（与 fastship orchestrator hooks 共存）：

fastship hooks 现在指向 orchestrator（orchestrator 内部 subprocess 调用 ship_verify_gate）。

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/hooks/forge_gate.py pre_edit",
            "timeout": 5,
            "statusMessage": "Forge: checking state protection..."
          },
          {
            "type": "command",
            "command": "python3 .claude/tools/fastship_orchestrator.py pre_edit",
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
            "command": "python3 .claude/hooks/forge_gate.py pre_edit",
            "timeout": 5,
            "statusMessage": "Forge: checking state protection..."
          },
          {
            "type": "command",
            "command": "python3 .claude/tools/fastship_orchestrator.py pre_edit",
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
            "command": "python3 .claude/tools/fastship_orchestrator.py pre_bash",
            "timeout": 10,
            "statusMessage": "Orchestrator: gates 0-5..."
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/hooks/forge_gate.py post_edit",
            "timeout": 5,
            "statusMessage": "Forge: detecting roadmap changes..."
          },
          {
            "type": "command",
            "command": "python3 .claude/tools/fastship_orchestrator.py post_edit",
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
            "command": "python3 .claude/hooks/forge_gate.py post_edit",
            "timeout": 5,
            "statusMessage": "Forge: detecting roadmap changes..."
          },
          {
            "type": "command",
            "command": "python3 .claude/tools/fastship_orchestrator.py post_edit",
            "timeout": 10,
            "statusMessage": "Orchestrator: auto-detect file writes..."
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/hooks/forge_gate.py post_bash",
            "timeout": 5,
            "statusMessage": "Forge: checking harvest reminders..."
          },
          {
            "type": "command",
            "command": "python3 .claude/tools/fastship_orchestrator.py post_bash",
            "timeout": 10,
            "statusMessage": "Orchestrator: auto-detect step completion..."
          }
        ]
      }
    ]
  }
}
```

**Hook 执行顺序**：同一个 matcher 下的 hooks 按数组顺序执行，全部必须通过（逻辑 AND）。Forge hooks 先跑，orchestrator 后跑（orchestrator 内部委托 ship_verify_gate）。

## 4. 创建项目目录

```bash
mkdir -p project-roadmap/features
```

## 5. 更新 .gitignore

```bash
echo ".claude/.forge-state.json" >> .gitignore
```

注意：`project-roadmap/` 应该加入 git 跟踪（团队共享）。

## 6. 验证安装

```bash
python3 .claude/hooks/forge_gate.py doctor
python3 .claude/hooks/forge_gate.py status
# 未初始化时预期：❌ No roadmap found. Run /forge init first.
```

## 7. 月度审计

上线复盘或合并前可运行：

```bash
python3 .claude/hooks/forge_gate.py audit-month 2026-05
python3 .claude/hooks/forge_gate.py audit-month 2026-05 --strict
```

`audit-month` 会对比当月 `docs/superpowers/plans/YYYY-MM-*.md`、`project-roadmap/features/*/metric.json`、`roadmap.json`。`--strict` 下任何 plan 缺 metric 都会失败。

## Gate 总览

| Gate | 转换 | 检查内容 |
|------|------|---------|
| G1 | → draft | metric.json 存在且合法 |
| G2 | draft → planned | fastship Phase 1 完成 (plan_ready) 或存在匹配的 plan artifact |
| G3 | planned → in_progress | 自动（/forge dev 触发） |
| G4 | in_progress → shipped | fastship Phase 3 完成 (test + e2e + knowledge) |
| G5 | shipped → measuring | 自动（/forge ship 内部） |
| G6 | measuring → concluded | harvest.json 存在且合法 |
| Audit | 月度审计 | plan / metric / roadmap 三方一致性 |
