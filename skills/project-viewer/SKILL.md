---
name: project-viewer
description: Launch a local web server to browse project structure and render markdown files in the browser. Use this skill when the user wants to view, browse, or explore a project's file tree or read markdown/README files in a visual way. Triggers include phrases like "show project structure", "browse files", "view readme", "open project viewer", "看项目结构", "看md文件", "浏览项目". Also use when the user wants a quick visual overview of a codebase or repository layout.
---

# Project Viewer Skill

Launches a lightweight local web server that provides:
- Interactive project directory tree (expandable/collapsible)
- Rendered markdown files with syntax highlighting
- Click any file in the tree to view it

## Usage

Run the bundled server script:

```bash
python3 /Users/apple/works/claude-skills/skills/project-viewer/scripts/server.py [project_root] [--port PORT]
```

- `project_root` defaults to current working directory
- `--port` defaults to 8877

The script will print a clickable URL in the terminal. Open it in a browser to browse the project.

## Key behaviors

1. Run the server in the **background** so the user can keep working: `python3 ... &`
2. Print the URL clearly: `http://localhost:8877`
3. To stop: `kill %1` or the PID printed at startup
4. The server auto-excludes: `.git`, `node_modules`, `__pycache__`, `.venv`, `dist`, `build`
