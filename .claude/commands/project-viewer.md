---
description: 启动本地 Web 服务浏览当前项目的目录结构和 Markdown 文件
---

启动 Project Viewer 本地服务，让用户在浏览器中查看当前项目的文件树和 Markdown 内容。

## 执行步骤

1. 在后台启动服务，将当前工作目录作为项目根目录：

```bash
python3 /Users/apple/works/claude-skills/skills/project-viewer/scripts/server.py "$(pwd)" --port 8877 &
```

2. 启动后，告诉用户：
   - 浏览器访问 `http://localhost:8877` 查看项目
   - 停止服务：`kill %1` 或用 `lsof -ti:8877 | xargs kill`

3. 如果端口 8877 被占用（启动失败），自动换用 8878、8879 依次尝试，最多重试 3 次。
