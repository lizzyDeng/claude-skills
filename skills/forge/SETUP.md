---
name: forge-setup
description: "在当前项目中安装 /forge skill（项目级 harness：roadmap + 收益回收）"
---

# /forge-setup

在当前项目中安装 /forge skill。按以下步骤执行：

## Step 1: 检查前置依赖

确认 fastship 已安装：

```bash
ls .claude/commands/fastship.md
ls .claude/hooks/ship_verify_gate.py
```

如果任一文件不存在，告知用户先运行 `/fastship-setup`，然后停止。

## Step 2: 复制 skill 定义

```bash
mkdir -p .claude/commands
cp /path/to/claude-skills/skills/forge/SKILL.md .claude/commands/forge.md
```

注意：`/path/to/claude-skills` 需替换为实际的 claude-skills 仓库路径。可通过检查现有 fastship.md 的来源推断。

## Step 3: 复制 gate 脚本

```bash
cp /path/to/claude-skills/skills/forge/hooks/forge_gate.py .claude/hooks/
```

## Step 4: 配置 hooks

读取当前 `.claude/settings.local.json`，将 forge 的 hook 配置合并进去。

**关键**：不覆盖 fastship 的 hooks，而是在同一个 matcher 的 `hooks` 数组中追加 forge 的 hook entry。forge hooks 应排在 fastship hooks 之前。

参照 `skills/forge/INSTALL.md` 中的完整 JSON 配置。

## Step 5: 创建项目目录

```bash
mkdir -p project-roadmap/features
```

## Step 6: 更新 .gitignore

检查 `.gitignore` 是否已包含 `.claude/.forge-state.json`，如果没有则追加：

```bash
grep -q ".forge-state.json" .gitignore || echo ".claude/.forge-state.json" >> .gitignore
```

## Step 7: 验证

```bash
python3 .claude/hooks/forge_gate.py status
```

预期输出：`❌ No roadmap found. Run /forge init first.`

## Step 8: 输出总结

告诉用户：

1. 安装完成，使用 `/forge init` 开始定义项目 roadmap
2. 完整命令列表：`/forge init` / `add` / `plan` / `dev` / `ship` / `harvest` / `status`
3. forge 包裹 fastship：进入 feature 开发时会自动调用 fastship 流程
