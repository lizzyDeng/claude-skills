# /forge 安装指南

推荐使用 `/forge-setup` 一键安装。以下是手动安装步骤。

## 0. 前置依赖

### 0.1 /fastship（🔴 必需）

forge 包裹 fastship，必须先安装 fastship。确认以下文件存在：

```bash
ls .claude/commands/fastship.md
ls .claude/hooks/ship_verify_gate.py
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

在 `.claude/settings.local.json` 中**合并**以下配置（与 fastship hooks 共存）：

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
            "command": "python3 .claude/hooks/ship_verify_gate.py pre_edit",
            "timeout": 5,
            "statusMessage": "Fastship: checking plan gate..."
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
            "command": "python3 .claude/hooks/ship_verify_gate.py pre_edit",
            "timeout": 5,
            "statusMessage": "Fastship: checking plan gate..."
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/hooks/ship_verify_gate.py pre_bash",
            "timeout": 10,
            "statusMessage": "Fastship: checking verification gates..."
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
            "command": "python3 .claude/hooks/ship_verify_gate.py post_edit",
            "timeout": 5,
            "statusMessage": "Fastship: tracking plan file..."
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
            "command": "python3 .claude/hooks/ship_verify_gate.py post_edit",
            "timeout": 5,
            "statusMessage": "Fastship: tracking plan file..."
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
            "command": "python3 .claude/hooks/ship_verify_gate.py post_bash",
            "timeout": 10,
            "statusMessage": "Fastship: tracking verification..."
          }
        ]
      }
    ]
  }
}
```

**Hook 执行顺序**：同一个 matcher 下的 hooks 按数组顺序执行，全部必须通过（逻辑 AND）。Forge hooks 先跑。

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
python3 .claude/hooks/forge_gate.py status
# 预期输出：❌ No roadmap found. Run /forge init first.
```

## Gate 总览

| Gate | 转换 | 检查内容 |
|------|------|---------|
| G1 | → draft | metric.json 存在且合法 |
| G2 | draft → planned | fastship Phase 1 完成 (plan_ready) |
| G3 | planned → in_progress | 自动（/forge dev 触发） |
| G4 | in_progress → shipped | fastship Phase 3 完成 (test + e2e + knowledge) |
| G5 | shipped → measuring | 自动（/forge ship 内部） |
| G6 | measuring → concluded | harvest.json 存在且合法 |
