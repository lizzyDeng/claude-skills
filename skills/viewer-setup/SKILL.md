---
name: viewer-setup
description: "Install /project-viewer as a global skill so it works in any project. Copies SKILL.md + server.py to ~/.claude/skills/project-viewer/."
---

# /viewer-setup — 安装 Project Viewer 为全局 Skill

将 project-viewer 安装到 `~/.claude/skills/project-viewer/`，之后在任何项目都能通过 `/project-viewer` 启动。

## 执行步骤

逐步执行以下命令：

### Step 1: 创建目录 + 复制 server 脚本

```bash
mkdir -p ~/.claude/skills/project-viewer/scripts
cp /Users/apple/works/claude-skills/skills/project-viewer/scripts/server.py \
   ~/.claude/skills/project-viewer/scripts/server.py
```

### Step 2: 生成全局 SKILL.md

源 SKILL.md 中 server.py 路径是本仓库的绝对路径，需要替换为全局路径：

```bash
sed 's|/Users/apple/works/claude-skills/skills/project-viewer/scripts/server.py|~/.claude/skills/project-viewer/scripts/server.py|g' \
  /Users/apple/works/claude-skills/skills/project-viewer/SKILL.md \
  > ~/.claude/skills/project-viewer/SKILL.md
```

### Step 3: 验证

```bash
echo "=== 文件检查 ==="
ls -la ~/.claude/skills/project-viewer/SKILL.md
ls -la ~/.claude/skills/project-viewer/scripts/server.py
echo ""
echo "=== 路径检查（应该是 ~/.claude 路径）==="
grep "server.py" ~/.claude/skills/project-viewer/SKILL.md
```

### Step 4: 输出结果

全部通过后输出：

```
✅ /project-viewer 已安装为全局 skill

使用方式：在任何项目目录下输入 /project-viewer 即可启动浏览器查看项目结构。

文件位置：
  ~/.claude/skills/project-viewer/SKILL.md
  ~/.claude/skills/project-viewer/scripts/server.py
```

如果 Step 3 有文件缺失，输出具体错误并提示用户检查源文件路径。
