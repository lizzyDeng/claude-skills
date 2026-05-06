---
description: 在当前项目或全局安装 /deeplearn skill（苏格拉底式技术文章学习）
---

你需要安装 /deeplearn skill。按以下步骤执行：

## Step 1: 询问安装方式

问用户想要哪种安装方式：

- **A. 全局安装**（推荐）— 安装到 `~/.claude/commands/`，所有项目可用
- **B. 项目安装** — 安装到当前项目的 `.claude/commands/`，仅当前项目可用

## Step 2: 安装 skill 定义

根据用户选择：

### A. 全局安装

```bash
mkdir -p ~/.claude/commands
cp /Users/apple/works/claude-skills/skills/deeplearn/SKILL.md ~/.claude/commands/deeplearn.md
```

### B. 项目安装

1. 确认当前目录是一个 git 仓库（`git rev-parse --show-toplevel`）
2. 执行：

```bash
mkdir -p .claude/commands
cp /Users/apple/works/claude-skills/skills/deeplearn/SKILL.md .claude/commands/deeplearn.md
```

## Step 3: 创建笔记输出目录

在目标位置创建笔记目录（全局安装放用户 home，项目安装放项目根目录）：

### A. 全局安装

笔记会写到使用 `/deeplearn` 时所在项目的 `deeplearn-notes/` 目录，无需预先创建。

### B. 项目安装

```bash
mkdir -p deeplearn-notes
echo "deeplearn-notes/" >> .gitignore
```

## Step 4: 验证

运行一次确认 skill 可被识别：

```bash
# 全局安装
ls ~/.claude/commands/deeplearn.md

# 项目安装
ls .claude/commands/deeplearn.md
```

## Step 5: 输出总结

告诉用户：

1. 安装完成，使用方式：
   - `/deeplearn https://example.com/some-article` — 从 URL 学习
   - `/deeplearn ./path/to/article.md` — 从本地文件学习
2. 学习笔记会自动保存到当前目录的 `deeplearn-notes/` 文件夹
3. 学习过程中随时可以说"结束"或"总结"来提前生成笔记
