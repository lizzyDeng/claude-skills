---
name: chunbing
description: "让春饼（布偶猫）常驻终端 statusline。安装后春饼会在命令行底部陪你写代码，心情随 context 使用率变化，还会随机做出各种猫咪动作。触发词：春饼、猫、statusline、陪我写代码。"
---

# 春饼 Statusline

让春饼常驻 Claude Code 终端底部，陪你写代码。

## 安装

将以下配置写入用户级或项目级 settings.json：

**用户级**（所有项目生效）：`~/.claude/settings.json`
**项目级**（仅当前项目）：`.claude/settings.json`

```json
{
  "statusLine": {
    "type": "command",
    "command": "/Users/apple/works/claude-skills/skills/chunbing/scripts/statusline.sh"
  }
}
```

## 功能

- 春饼的表情随 context 使用率变化：😸 → 😺 → 😿 → 🙀
- 随机猫咪动作：踩键盘、盯光标、舔爪子、蹭手...
- 猫爪进度条 🐾 显示 context 使用率
- 显示当前模型、目录、费用

## 卸载

删除 settings.json 中的 `statusLine` 字段，或运行 `/statusline clear`。
