---
description: 安装春饼（statusline + 炼狱篇情绪感知），让春饼常驻终端陪你写代码
---

将春饼全套功能安装到用户级配置中：
- **statusline**：春饼常驻终端底部
- **炼狱篇**：感知七宗罪情绪，温柔提醒用户冷静

## 执行步骤

### 1. Statusline

读取 `~/.claude/settings.json`，将 `statusLine` 设置为 wrapper（兼容已有 claude-hud）：

```json
{
  "statusLine": {
    "type": "command",
    "command": "/Users/apple/works/claude-skills/skills/chunbing/scripts/statusline-wrapper.sh"
  }
}
```

如果已有 `statusLine` 配置且不是 wrapper，提醒用户会覆盖，确认后再写入。

### 2. 炼狱篇 Hook

在 `~/.claude/settings.json` 的 `hooks` 中添加 `UserPromptSubmit` hook：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /Users/apple/works/claude-skills/skills/chunbing/scripts/purgatorio.py",
            "timeout": 5,
            "statusMessage": "春饼在感知你的情绪..."
          }
        ]
      }
    ]
  }
}
```

**合并规则**：
- 不覆盖已有 hooks 配置，合并到 `hooks.UserPromptSubmit` 数组中
- 检查是否已存在 purgatorio.py 的 hook，避免重复
- 保留所有其他配置

### 3. 验证

安装完成后告诉用户：
- 春饼已就位，重启 Claude Code 或开新会话即可生效
- statusline：春饼常驻底部，心情随 context 变化
- 炼狱篇：当检测到愤怒、懒惰、贪婪等情绪时，春饼会温柔提醒
