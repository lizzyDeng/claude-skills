#!/usr/bin/env python3
"""Project Viewer - lightweight local server for browsing project structure and markdown files."""

import http.server
import json
import os
import sys
import argparse
import html
import urllib.parse
from pathlib import Path

EXCLUDE_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', '.next', '.cache', '.DS_Store'}
MD_EXTENSIONS = {'.md', '.mdx', '.markdown'}
CODE_EXTENSIONS = {
    '.py': 'python', '.js': 'javascript', '.ts': 'typescript', '.tsx': 'tsx', '.jsx': 'jsx',
    '.json': 'json', '.yaml': 'yaml', '.yml': 'yaml', '.toml': 'toml',
    '.sh': 'bash', '.bash': 'bash', '.zsh': 'bash',
    '.html': 'html', '.css': 'css', '.scss': 'scss',
    '.rs': 'rust', '.go': 'go', '.java': 'java', '.kt': 'kotlin',
    '.rb': 'ruby', '.php': 'php', '.swift': 'swift',
    '.c': 'c', '.cpp': 'cpp', '.h': 'c', '.hpp': 'cpp',
    '.sql': 'sql', '.graphql': 'graphql', '.proto': 'protobuf',
    '.dockerfile': 'dockerfile', '.xml': 'xml', '.svg': 'xml',
}
TEXT_EXTENSIONS = {'.txt', '.log', '.env', '.gitignore', '.dockerignore', '.editorconfig', '.prettierrc', '.eslintrc'} | MD_EXTENSIONS | set(CODE_EXTENSIONS.keys())

PROJECT_ROOT = '.'

def scan_tree(root: str) -> dict:
    root_path = Path(root).resolve()
    def _scan(p: Path) -> list:
        items = []
        try:
            entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            return items
        for entry in entries:
            if entry.name in EXCLUDE_DIRS or entry.name.startswith('.'):
                continue
            rel = str(entry.relative_to(root_path))
            if entry.is_dir():
                children = _scan(entry)
                items.append({'name': entry.name, 'path': rel, 'type': 'dir', 'children': children})
            else:
                items.append({'name': entry.name, 'path': rel, 'type': 'file'})
        return items
    return {'name': root_path.name, 'path': '.', 'type': 'dir', 'children': _scan(root_path)}

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Project Viewer</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.0/marked.min.js"></script>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace; background:#0d1117; color:#c9d1d9; display:flex; height:100vh; }
  #sidebar { width:300px; min-width:220px; max-width:500px; background:#161b22; border-right:1px solid #30363d; overflow-y:auto; padding:12px 0; resize:horizontal; }
  #sidebar h2 { padding:8px 16px; font-size:14px; color:#58a6ff; border-bottom:1px solid #30363d; margin-bottom:8px; }
  .tree-item { cursor:pointer; padding:4px 8px 4px calc(var(--depth)*16px + 12px); font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .tree-item:hover { background:#1f2937; }
  .tree-item.active { background:#1f6feb33; color:#58a6ff; }
  .tree-item .icon { margin-right:6px; font-size:12px; }
  .dir-toggle { display:inline-block; width:14px; text-align:center; font-size:10px; margin-right:2px; color:#8b949e; }
  .hidden { display:none; }
  #content { flex:1; overflow-y:auto; padding:32px 48px; }
  #content.markdown-body { line-height:1.7; }
  #content h1,#content h2,#content h3 { color:#e6edf3; margin:24px 0 12px; border-bottom:1px solid #30363d; padding-bottom:6px; }
  #content h1 { font-size:28px; } #content h2 { font-size:22px; } #content h3 { font-size:18px; }
  #content p { margin:8px 0; }
  #content pre { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:16px; overflow-x:auto; margin:12px 0; }
  #content code { font-family:'Fira Code',monospace; font-size:13px; }
  #content p code { background:#1f2937; padding:2px 6px; border-radius:4px; font-size:13px; }
  #content a { color:#58a6ff; }
  #content ul,#content ol { padding-left:24px; margin:8px 0; }
  #content blockquote { border-left:3px solid #3b82f6; padding-left:16px; color:#8b949e; margin:12px 0; }
  #content table { border-collapse:collapse; margin:12px 0; }
  #content th,#content td { border:1px solid #30363d; padding:8px 12px; text-align:left; }
  #content th { background:#161b22; }
  #content img { max-width:100%; }
  .welcome { color:#8b949e; margin-top:20vh; text-align:center; }
  .welcome h1 { border:none; color:#58a6ff; font-size:24px; }
  .file-path { font-size:12px; color:#8b949e; margin-bottom:16px; padding:8px 12px; background:#161b22; border-radius:6px; font-family:monospace; }
</style>
</head>
<body>
<div id="sidebar">
  <h2 id="project-name"></h2>
  <div id="tree"></div>
</div>
<div id="content" class="markdown-body">
  <div class="welcome"><h1>📂 Project Viewer</h1><p>Click a file in the sidebar to view it</p></div>
</div>
<script>
let treeData = null;

async function init() {
  const res = await fetch('/api/tree');
  treeData = await res.json();
  document.getElementById('project-name').textContent = '📁 ' + treeData.name;
  document.getElementById('tree').innerHTML = renderTree(treeData.children, 0);
  // auto-open README if exists
  const readme = findFile(treeData.children, /^readme\.md$/i);
  if (readme) loadFile(readme);
}

function findFile(items, pattern) {
  for (const item of items) {
    if (item.type === 'file' && pattern.test(item.name)) return item.path;
    if (item.type === 'dir' && item.children) {
      const found = findFile(item.children, pattern);
      if (found) return found;
    }
  }
  return null;
}

function renderTree(items, depth) {
  return items.map(item => {
    if (item.type === 'dir') {
      const id = 'dir-' + item.path.replace(/[^a-zA-Z0-9]/g, '_');
      return `<div class="tree-item" style="--depth:${depth}" onclick="toggleDir('${id}',this)">
        <span class="dir-toggle" id="toggle-${id}">▶</span><span class="icon">📁</span>${item.name}
      </div><div id="${id}" class="hidden">${renderTree(item.children, depth+1)}</div>`;
    }
    const icon = getIcon(item.name);
    return `<div class="tree-item" style="--depth:${depth}" onclick="loadFile('${item.path}')" data-path="${item.path}">
      <span class="dir-toggle"></span><span class="icon">${icon}</span>${item.name}
    </div>`;
  }).join('');
}

function getIcon(name) {
  const ext = '.' + name.split('.').pop().toLowerCase();
  if (['.md','.mdx'].includes(ext)) return '📝';
  if (['.js','.ts','.jsx','.tsx'].includes(ext)) return '🟨';
  if (['.py'].includes(ext)) return '🐍';
  if (['.json'].includes(ext)) return '📋';
  if (['.yaml','.yml','.toml'].includes(ext)) return '⚙️';
  if (['.sh','.bash'].includes(ext)) return '💻';
  if (['.css','.scss'].includes(ext)) return '🎨';
  if (['.html'].includes(ext)) return '🌐';
  if (['.rs'].includes(ext)) return '🦀';
  if (['.go'].includes(ext)) return '🔵';
  return '📄';
}

function toggleDir(id, el) {
  const div = document.getElementById(id);
  const toggle = document.getElementById('toggle-' + id);
  div.classList.toggle('hidden');
  toggle.textContent = div.classList.contains('hidden') ? '▶' : '▼';
}

async function loadFile(path) {
  document.querySelectorAll('.tree-item').forEach(e => e.classList.remove('active'));
  document.querySelector(`.tree-item[data-path="${path}"]`)?.classList.add('active');
  const res = await fetch('/api/file?path=' + encodeURIComponent(path));
  const data = await res.json();
  const content = document.getElementById('content');
  const pathBar = `<div class="file-path">${data.path}</div>`;
  if (data.type === 'markdown') {
    content.innerHTML = pathBar + marked.parse(data.content);
    content.querySelectorAll('pre code').forEach(b => hljs.highlightElement(b));
  } else if (data.type === 'code') {
    const lang = data.language || '';
    const highlighted = lang ? hljs.highlight(data.content, {language: lang}).value : hljs.highlightAuto(data.content).value;
    content.innerHTML = pathBar + `<pre><code class="hljs">${highlighted}</code></pre>`;
  } else if (data.type === 'text') {
    content.innerHTML = pathBar + `<pre><code>${escapeHtml(data.content)}</code></pre>`;
  } else {
    content.innerHTML = pathBar + `<p style="color:#8b949e">Binary file - cannot display</p>`;
  }
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

init();
</script>
</body>
</html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress logs

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/' or parsed.path == '':
            self._respond(200, 'text/html', HTML_TEMPLATE)
        elif parsed.path == '/api/tree':
            tree = scan_tree(PROJECT_ROOT)
            self._respond(200, 'application/json', json.dumps(tree))
        elif parsed.path == '/api/file':
            params = urllib.parse.parse_qs(parsed.query)
            fpath = params.get('path', [''])[0]
            self._serve_file(fpath)
        else:
            self._respond(404, 'text/plain', 'Not found')

    def _serve_file(self, rel_path):
        root = Path(PROJECT_ROOT).resolve()
        target = (root / rel_path).resolve()
        # security: ensure within root
        if not str(target).startswith(str(root)):
            self._respond(403, 'application/json', json.dumps({'error': 'forbidden'}))
            return
        if not target.is_file():
            self._respond(404, 'application/json', json.dumps({'error': 'not found'}))
            return
        ext = target.suffix.lower()
        try:
            content = target.read_text(encoding='utf-8', errors='replace')
            if ext in MD_EXTENSIONS:
                ftype = 'markdown'
                lang = None
            elif ext in CODE_EXTENSIONS:
                ftype = 'code'
                lang = CODE_EXTENSIONS[ext]
            elif ext in TEXT_EXTENSIONS or not target.suffix:
                ftype = 'text'
                lang = None
            else:
                ftype = 'binary'
                lang = None
                content = ''
        except Exception:
            ftype = 'binary'
            lang = None
            content = ''
        data = {'path': rel_path, 'type': ftype, 'content': content}
        if lang:
            data['language'] = lang
        self._respond(200, 'application/json', json.dumps(data))

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body.encode('utf-8') if isinstance(body, str) else body)

def main():
    global PROJECT_ROOT
    parser = argparse.ArgumentParser(description='Project Viewer')
    parser.add_argument('root', nargs='?', default='.', help='Project root directory')
    parser.add_argument('--port', type=int, default=8877, help='Port (default: 8877)')
    args = parser.parse_args()
    PROJECT_ROOT = os.path.abspath(args.root)
    server = http.server.HTTPServer(('0.0.0.0', args.port), Handler)
    print(f'\n  🔍 Project Viewer running at:\n')
    print(f'     http://localhost:{args.port}\n')
    print(f'  📁 Serving: {PROJECT_ROOT}')
    print(f'  Press Ctrl+C to stop\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  Stopped.')
        server.server_close()

if __name__ == '__main__':
    main()
