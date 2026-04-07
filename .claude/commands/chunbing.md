---
description: 安装春饼 statusline，让春饼常驻终端陪你写代码
---

将春饼 statusline 安装到用户级配置中，让春饼在所有项目的 Claude Code 终端底部常驻。

## 执行步骤

1. 读取 `~/.claude/settings.json`（如果存在）
2. 将 `statusLine` 字段设置为：

```json
{
  "statusLine": {
    "type": "command",
    "command": "/Users/apple/works/claude-skills/skills/chunbing/scripts/statusline.sh"
  }
}
```

3. 如果已有 `statusLine` 配置，提醒用户会覆盖，确认后再写入
4. 保留 settings.json 中所有其他配置不变
5. 安装完成后告诉用户：春饼已就位，重启 Claude Code 或开新会话即可看到春饼在终端底部陪你写代码
