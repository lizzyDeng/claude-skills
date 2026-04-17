---
description: 在当前项目中安装 /fastship skill 的完整工具链（skill 定义 + hooks + E2E 工具）
---

你需要在当前项目中安装 /fastship skill 的完整工具链。按以下步骤执行：

## Step 1: 检测项目

1. 确认当前目录是一个 git 仓库（`git rev-parse --show-toplevel`）
2. 检测项目技术栈（查看 Cargo.toml / package.json / pyproject.toml / go.mod 等）
3. 输出检测结果告知用户

## Step 2: 安装 skill 定义

1. 创建 `.claude/commands/` 目录
2. 将 `/Users/apple/works/claude-skills/skills/fastship/SKILL.md` 复制到 `.claude/commands/fastship.md`

## Step 3: 安装 hooks

1. 创建 `.claude/hooks/` 目录
2. 将 `/Users/apple/works/claude-skills/skills/fastship/hooks/ship_verify_gate.py` 复制到 `.claude/hooks/ship_verify_gate.py`

## Step 4: 安装 E2E 工具

1. 创建 `tests/` 和 `tests/e2e_scenarios/` 目录
2. 将 `/Users/apple/works/claude-skills/skills/fastship/e2e/e2e_runner.py` 复制到 `tests/e2e_runner.py`
3. 将 `/Users/apple/works/claude-skills/skills/fastship/e2e/e2e_gate.py` 复制到 `tests/e2e_gate.py`
4. 将 `/Users/apple/works/claude-skills/skills/fastship/e2e/scenario_template.json` 复制到 `tests/e2e_scenarios/_template.json`

## Step 5: 配置 hooks

读取当前项目的 `.claude/settings.local.json`（如果存在），将以下 hooks 配置**合并**进去（不覆盖已有配置）：

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/hooks/ship_verify_gate.py post_bash",
            "timeout": 10,
            "statusMessage": "Tracking verification..."
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/hooks/ship_verify_gate.py pre_edit",
            "timeout": 5,
            "statusMessage": "Checking state file protection..."
          }
        ]
      },
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/hooks/ship_verify_gate.py pre_edit",
            "timeout": 5,
            "statusMessage": "Checking state file protection..."
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
            "statusMessage": "Checking verification gate..."
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
- 不要重复添加（检查 command 字段是否已包含 `ship_verify_gate.py`）
- 保留所有已有的 permissions 和其他配置

## Step 6: 更新 .gitignore

在项目 `.gitignore` 中追加（如果不存在）：

```
.claude/.ship-verify-state.json
```

## Step 7: 验证

运行 `python3 .claude/hooks/ship_verify_gate.py status` 确认安装成功。

## Step 8: 输出总结

告诉用户：
1. 检测到的技术栈
2. 安装了哪些文件（包括 `.claude/commands/fastship.md` skill 定义）
3. 配置了哪些 hooks
4. 提醒：`/fastship` 即可开始使用
5. 提醒：`e2e_runner.py` 是通用模板，如项目有特殊需求（如 SSE 流式、日志 pipeline 提取），可按需定制
