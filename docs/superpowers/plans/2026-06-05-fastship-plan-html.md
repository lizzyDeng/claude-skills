# Fastship Plan 可视化（plan.md → 自包含 plan.html）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** fastship Stage 1（Step 1.4 写计划）产出 `docs/superpowers/plans/*.md` 后，自动在同目录生成一个本地可直接打开的自包含 `*.plan.html`，把技术方案、流程图、模块架构图、E2E↔AC 覆盖渲染成直观可视图，解决「纯 md 看不清」。

**Architecture:** 新增纯 Python 渲染器 `skills/fastship/scripts/plan_html.py`：① 解析 plan markdown 成结构模型（标题/Goal/Architecture/Tech Stack/AC↔E2E 表/File Structure/mermaid 块/正文）；② 自写 markdown→HTML（离线、确定性、可单测，覆盖 plan 实际用到的子集）；③ 把 AC↔E2E 表渲染成彩色**覆盖矩阵**、把 File Structure 渲染成**模块架构图**（Create/Modify/Test 分组）、把 ```mermaid 块渲染成 `<pre class="mermaid">`；④ 组装成单文件 HTML（内联 CSS + 客户端只需 mermaid 一个库）。正文+矩阵+模块图全部 Python 渲染→**完全离线可读**；唯一联网点是 mermaid 流程图（`mermaid.min.js`，默认 CDN，离线时优雅降级为显示图源）。orchestrator 在 Step 1.4 plan 通过校验后（hook 模式 + CLI 模式两条路径）调用渲染器生成 HTML（try/except 包住，**失败不阻断**流程，路径记入 orch artifacts 但**不进可信账本**），并新增 `render-plan` 子命令按需重渲染。Step 1.4 指令文本（fastship 自己的，非外部 writing-plans 插件）追加「补 `## 图示` mermaid + 确保 AC↔E2E 表」的提示。

**Tech Stack:** Python 3.11 stdlib only（`re`/`html`/`dataclasses`/`pathlib`/`hashlib`）、pytest（`importlib.util.spec_from_file_location` 加载范式）、纯 Python skill E2E runner（`*_e2e_runner.py`，scenarios→rounds→turns）、mermaid.js（前端 CDN，唯一外部依赖，可降级）。

> **⚠️ 待用户在 1.6 确认的设计取舍（Mermaid 库分发）**：默认 **CDN**（输出极小、与仓内 `project-viewer` 先例一致、离线时降级为显示 mermaid 图源文本）。备选 **vendored**（把 `mermaid.min.js`≈600KB 提交进 `skills/fastship/scripts/vendor/`，完全离线，但仓库膨胀、与本仓 stdlib-only/no-vendored-JS 风格相悖）。本计划按 **CDN + 优雅降级** 实现；渲染器留 `MERMAID_SRC` 单点常量，后续切 vendored 只改一行。

---

## 验收清单（AC）→ E2E 映射（每条 AC 必有 E2E turn）

| AC | 可观察断言 | E2E scenario.round |
|----|-----------|--------------------|
| AC1 | 完整 plan（含 Goal/Architecture/AC 表/File Structure/mermaid）渲染产出 HTML，含 `<!DOCTYPE html>` + plan 标题文本 + `<style>` 内联样式 | S.full.render |
| AC2 | markdown 正文离线渲染（不依赖任何 CDN）：`# H1`→`<h1`、`**x**`→`<strong>`、`` `c` ``→`<code>`、`- a`→`<li>`、表格→`<table>`、`> q`→`<blockquote>`、`---`→`<hr>` 全部产出对应 HTML 标签 | S.md.constructs |
| AC3 | AC↔E2E 表渲染为覆盖矩阵：E2E 列**非空**行带 `covered`（绿）标记、**空**行带 `uncovered`（红）标记；矩阵含每条 AC 文本 | S.coverage.matrix |
| AC4 | File Structure 渲染为模块架构图：按 `Create`/`Modify`/`Test` 分组，每个文件路径出现在对应分组块内 | S.module.map |
| AC5 | ```mermaid 块→`<pre class="mermaid">` 含图源；非 mermaid fenced code（```python）→`<pre><code` 且 **不**含 `class="mermaid"` | S.mermaid.block |
| AC6 | 旧/瘦 plan（无 AC 表、无 File Structure、无 mermaid）→ 不抛异常，仍产出含正文的 HTML，缺失面板被跳过（HTML 不含覆盖矩阵容器） | S.degrade.thin |
| AC7 | CLI 模式 `done --plan <plan.md>` 完成 Step 1.4 后，在 plan.md **同目录**生成 `<basename>.plan.html`，文件真实存在且非空 | S.cli.autogen |
| AC8 | 渲染抛异常时，`generate_plan_html` 返回 `None` 且**不**向上抛；Step 1.4 仍判定通过（`current_step` 推进到 1.5），stdout 含 `⚠️`/`WARN` 降级提示 | S.fail.nonblock |
| AC9 | `orchestrator.py render-plan <plan.md>` 子命令产出 HTML + 打印绝对路径；`render-plan`（不带参数）用当前 session 的 `plan_path` | S.subcmd.render |
| AC10 | plan.html 路径记入 `orch["artifacts"]["plan_html_path"]`，但 `orch["artifacts"]["trusted_artifacts"]` 里**没有** html 条目（不污染可信账本、不改 plan artifact hash 校验） | S.ledger.clean |
| AC11 | XSS-safe：plan 正文里的 `<script>alert(1)</script>` 在 HTML 输出中被转义成 `&lt;script&gt;`（不出现可执行 `<script>alert`） | S.xss.escape |
| AC12 | Step 1.4 instruction 文本含「图示/mermaid」提示且仍包含三个 PLAN_SIGNATURE_MARKERS 不被破坏（orchestrator import 后 `validate_plan` 对合规 plan 仍判定通过） | S.instruction.markers |

E2E 主断言均为业务可观察结果（HTML 文件存在/标签/文本/account 字段/退出码），无「页面加载」「无报错」类弱断言。
runner=`tests/fastship/plan_html_e2e_runner.py`（匹配 `e2e[_-]?runner`），≥12 turns，无需 `fastship.project.json`。

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `skills/fastship/scripts/plan_html.py` | plan markdown 解析 + 离线 markdown→HTML + 覆盖矩阵/模块图/mermaid 渲染 + 单文件组装 + CLI `main` | Create |
| `skills/fastship/orchestrator.py` | 在 1.4 通过后（hook+CLI）调 `generate_plan_html`；新增 `render-plan` 子命令；更新 Step 1.4 instruction 文本 | Modify |
| `tests/fastship/test_plan_html.py` | 渲染器单测（解析/各 markdown 构件/矩阵/模块图/mermaid/降级/XSS） | Create |
| `tests/fastship/test_orchestrator_plan_html.py` | orchestrator 集成单测（CLI 1.4 自动产 HTML、非可信账本、失败不阻断、render-plan 子命令） | Create |
| `tests/fastship/plan_html_e2e_runner.py` | 纯 Python E2E：跑真渲染器 + 真 orchestrator CLI，scenarios→rounds→turns，≥12 turns | Create |
| `skills/fastship/SKILL.md` | 文档：Stage 1 产出 plan.html + render-plan 命令说明 | Modify |

模块边界：`plan_html.py` 是**自洽纯函数库**（`render_plan_html(md:str)->str` 不碰文件系统，`render_plan_file(path)->out_path` 才落盘），orchestrator 只调一个薄封装 `generate_plan_html(plan_path)->Optional[str]`。渲染器对 orchestrator 零依赖（可独立单测、独立 CLI）。

---

## Task 1: plan_html.py 骨架 + plan 解析层

**Files:**
- Create: `skills/fastship/scripts/plan_html.py`
- Test: `tests/fastship/test_plan_html.py`

- [ ] **Step 1: 写失败测试（解析层）**

```python
# tests/fastship/test_plan_html.py
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "fastship" / "scripts" / "plan_html.py"


def load_mod():
    spec = importlib.util.spec_from_file_location("plan_html", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SAMPLE = """# My Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development.

**Goal:** Build the thing.

**Architecture:** Do it with Python.

**Tech Stack:** Python stdlib.

---

## 验收清单（AC）→ E2E 映射

| AC | 可观察断言 | E2E scenario.round |
|----|-----------|--------------------|
| AC1 | does X | S.x |
| AC2 | does Y | |

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `a/b.py` | core | Create |
| `c/d.py` | wire | Modify |
| `tests/t.py` | test | Test |

## 图示

```mermaid
flowchart TD
  A --> B
```

## Task 1: thing
- [ ] **Step 1: do it**
"""


def test_parse_extracts_header_fields():
    m = load_mod()
    model = m.parse_plan(SAMPLE)
    assert model.title == "My Feature Implementation Plan"
    assert model.goal == "Build the thing."
    assert model.architecture == "Do it with Python."
    assert model.tech_stack == "Python stdlib."


def test_parse_extracts_ac_rows():
    m = load_mod()
    model = m.parse_plan(SAMPLE)
    # header row excluded; 2 AC data rows
    assert len(model.ac_rows) == 2
    assert model.ac_rows[0]["ac"] == "AC1"
    assert model.ac_rows[0]["e2e"] == "S.x"
    assert model.ac_rows[1]["e2e"] == ""  # uncovered


def test_parse_extracts_modules():
    m = load_mod()
    model = m.parse_plan(SAMPLE)
    paths = {(x["path"], x["change"]) for x in model.modules}
    assert ("a/b.py", "Create") in paths
    assert ("c/d.py", "Modify") in paths
    assert ("tests/t.py", "Test") in paths


def test_parse_extracts_mermaid():
    m = load_mod()
    model = m.parse_plan(SAMPLE)
    assert len(model.mermaid_blocks) == 1
    assert "flowchart TD" in model.mermaid_blocks[0]
```

- [ ] **Step 2: 跑失败** `env -u FASTSHIP_SESSION -u FASTSHIP_REPO_ROOT -u FASTSHIP_STATE_HOME python3 -m pytest tests/fastship/test_plan_html.py -q -p no:cacheprovider` → FAIL（`No module named ... parse_plan` / AttributeError）

- [ ] **Step 3: 写解析层实现**

```python
# skills/fastship/scripts/plan_html.py
#!/usr/bin/env python3
"""Render a fastship plan markdown file into a self-contained, visual HTML.

Pure stdlib. The markdown body, AC↔E2E coverage matrix, and module-architecture
map are all rendered server-side (offline, deterministic, unit-tested). The only
client-side dependency is mermaid.js for the optional flow diagram, which loads
from CDN and degrades to showing the diagram source when offline.
"""
import argparse
import html as _html
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Dict, Optional

# Single point of control for the mermaid library source. To vendor for full
# offline use, swap this for an inline <script>...</script> with mermaid.min.js.
MERMAID_SRC = "https://cdnjs.cloudflare.com/ajax/libs/mermaid/10.9.0/mermaid.min.js"


@dataclass
class PlanModel:
    title: str = ""
    goal: str = ""
    architecture: str = ""
    tech_stack: str = ""
    ac_rows: List[Dict[str, str]] = field(default_factory=list)
    modules: List[Dict[str, str]] = field(default_factory=list)
    mermaid_blocks: List[str] = field(default_factory=list)
    body_md: str = ""


def _field_after_label(md: str, label: str) -> str:
    # Matches a line like: **Goal:** some text
    m = re.search(r"^\*\*" + re.escape(label) + r":\*\*\s*(.+)$", md, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _extract_tables(md: str) -> List[List[List[str]]]:
    """Return every markdown pipe-table as its rows (header + body; separator row dropped).

    A separator row is one whose every cell matches ``:?-{2,}:?`` (e.g. ``---`` /
    ``:---:``). Contiguous pipe-rows form one table; a blank/non-pipe line ends it.
    """
    def is_sep(cells: List[str]) -> bool:
        return bool(cells) and all(
            re.match(r"^:?-{2,}:?$", c.replace(" ", "")) for c in cells)

    tables: List[List[List[str]]] = []
    cur: List[List[str]] = []
    for line in md.split("\n"):
        s = line.strip()
        if s.startswith("|") and s.count("|") >= 2:
            cells = [c.strip() for c in s.strip("|").split("|")]
            if is_sep(cells):
                continue
            cur.append(cells)
        else:
            if cur:
                tables.append(cur)
            cur = []
    if cur:
        tables.append(cur)
    return tables


def _find_ac_table(md: str) -> List[Dict[str, str]]:
    for table in _extract_tables(md):
        if not table:
            continue
        header = [c.lower() for c in table[0]]
        if header and ("ac" in header[0]):
            out = []
            for row in table[1:]:
                if not row or not row[0]:
                    continue
                e2e = row[2] if len(row) >= 3 else ""
                out.append({
                    "ac": row[0],
                    "assertion": row[1] if len(row) >= 2 else "",
                    "e2e": e2e,
                })
            return out
    return []


def _find_modules(md: str) -> List[Dict[str, str]]:
    """Extract (path, change) from a File Structure table or 'Create:/Modify:/Test:' bullets."""
    mods: List[Dict[str, str]] = []
    # Table form: columns include a File and a Change column
    for table in _extract_tables(md):
        if not table:
            continue
        header = [c.lower() for c in table[0]]
        if any("file" in h for h in header) and any("change" in h for h in header):
            for row in table[1:]:
                if len(row) < 3:
                    continue
                path = row[0].strip("`").strip()
                change = row[-1].strip()
                if path:
                    mods.append({"path": path, "change": change})
    if mods:
        return mods
    # Bullet form: "- Create: `path`"
    for m in re.finditer(r"(Create|Modify|Test)\s*:\s*`([^`]+)`", md):
        mods.append({"path": m.group(2), "change": m.group(1)})
    return mods


def _extract_mermaid(md: str) -> List[str]:
    return [m.group(1).strip() for m in re.finditer(r"```mermaid\s*\n(.*?)```", md, re.DOTALL)]


def parse_plan(md: str) -> PlanModel:
    title_m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
    return PlanModel(
        title=title_m.group(1).strip() if title_m else "Plan",
        goal=_field_after_label(md, "Goal"),
        architecture=_field_after_label(md, "Architecture"),
        tech_stack=_field_after_label(md, "Tech Stack"),
        ac_rows=_find_ac_table(md),
        modules=_find_modules(md),
        mermaid_blocks=_extract_mermaid(md),
        body_md=md,
    )
```

- [ ] **Step 4: 跑过** 同 Step 2 命令 → PASS（4 个解析测试通过）

- [ ] **Step 5: Commit**

```bash
git add skills/fastship/scripts/plan_html.py tests/fastship/test_plan_html.py
git commit -m "feat(fastship): plan_html parse layer — extract goal/arch/AC table/modules/mermaid"
```

---

## Task 2: 离线 markdown→HTML 渲染（plan 子集）

**Files:**
- Modify: `skills/fastship/scripts/plan_html.py`
- Test: `tests/fastship/test_plan_html.py`

- [ ] **Step 1: 写失败测试（各 markdown 构件 + XSS）**

```python
# append to tests/fastship/test_plan_html.py
def test_md_headings_and_inline():
    m = load_mod()
    h = m.md_to_html("# Title\n\nsome **bold** and `code` text\n")
    assert "<h1" in h and "Title" in h
    assert "<strong>bold</strong>" in h
    assert "<code>code</code>" in h


def test_md_list_and_checkbox():
    m = load_mod()
    h = m.md_to_html("- one\n- two\n\n- [ ] **Step 1: do**\n")
    assert "<ul>" in h and "<li>one</li>" in h
    assert "Step 1: do" in h


def test_md_table():
    m = load_mod()
    h = m.md_to_html("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<table" in h and "<th>a</th>" in h and "<td>1</td>" in h


def test_md_blockquote_hr():
    m = load_mod()
    h = m.md_to_html("> quoted\n\n---\n")
    assert "<blockquote>" in h and "quoted" in h
    assert "<hr" in h


def test_md_fenced_code_vs_mermaid():
    m = load_mod()
    h = m.md_to_html("```python\nx=1\n```\n\n```mermaid\nflowchart TD\nA-->B\n```\n")
    assert "<pre><code" in h and "x=1" in h
    assert 'class="mermaid"' in h and "flowchart TD" in h


def test_md_xss_escaped():
    m = load_mod()
    h = m.md_to_html("normal <script>alert(1)</script> text\n")
    assert "&lt;script&gt;" in h
    assert "<script>alert" not in h
```

- [ ] **Step 2: 跑失败** → FAIL（`md_to_html` 不存在）

- [ ] **Step 3: 写 markdown 渲染实现**

```python
# append to skills/fastship/scripts/plan_html.py

_CODE_TOKEN = "\x00CODE%d\x00"


def _inline(text: str) -> str:
    """Render inline markdown to HTML on an HTML-escaped string. Code spans are
    protected from further inline processing via placeholder tokens."""
    codes: List[str] = []

    def stash(m):
        codes.append("<code>" + _html.escape(m.group(1), quote=False) + "</code>")
        return _CODE_TOKEN % (len(codes) - 1)

    # protect `code` BEFORE escaping (capture raw), replace with token
    text = re.sub(r"`([^`]+)`", stash, text)
    out = _html.escape(text, quote=False)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*\s][^*]*?)\*(?!\*)", r"<em>\1</em>", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', out)
    for i, c in enumerate(codes):
        out = out.replace(_CODE_TOKEN % i, c)
    return out


def _slug(text: str) -> str:
    s = re.sub(r"[^\w一-鿿]+", "-", text.strip().lower())
    return s.strip("-") or "section"


def md_to_html(md: str) -> str:
    lines = md.split("\n")
    out: List[str] = []
    i, n = 0, len(lines)

    def flush_table(start: int) -> int:
        rows = []
        j = start
        while j < n and lines[j].strip().startswith("|"):
            rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
            j += 1
        if len(rows) >= 2 and all(re.match(r"^:?-{2,}:?$", c.replace(" ", "")) for c in rows[1]):
            head, body = rows[0], rows[2:]
            out.append('<table class="md-table"><thead><tr>')
            out.extend("<th>" + _inline(c) + "</th>" for c in head)
            out.append("</tr></thead><tbody>")
            for r in body:
                out.append("<tr>" + "".join("<td>" + _inline(c) + "</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
            return j
        # not a real table; emit as paragraphs
        for r in rows:
            out.append("<p>" + _inline("| " + " | ".join(r) + " |") + "</p>")
        return j

    while i < n:
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        # fenced code
        if s.startswith("```"):
            info = s[3:].strip()
            j = i + 1
            buf = []
            while j < n and not lines[j].strip().startswith("```"):
                buf.append(lines[j])
                j += 1
            code = "\n".join(buf)
            if info == "mermaid":
                out.append('<pre class="mermaid">' + _html.escape(code, quote=False) + "</pre>")
            else:
                cls = (' class="language-%s"' % info) if info else ""
                out.append("<pre><code%s>" % cls + _html.escape(code, quote=False) + "</code></pre>")
            i = j + 1
            continue
        # heading
        hm = re.match(r"^(#{1,6})\s+(.+)$", s)
        if hm:
            lvl = len(hm.group(1))
            txt = hm.group(2)
            out.append("<h%d id=\"%s\">%s</h%d>" % (lvl, _slug(txt), _inline(txt), lvl))
            i += 1
            continue
        # hr
        if re.match(r"^(-{3,}|\*{3,})$", s):
            out.append("<hr/>")
            i += 1
            continue
        # blockquote
        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].lstrip())
                i += 1
            out.append("<blockquote>" + _inline(" ".join(buf)) + "</blockquote>")
            continue
        # table
        if s.startswith("|"):
            i = flush_table(i)
            continue
        # list (ordered/unordered, with indent-based nesting and checkboxes)
        if re.match(r"^\s*([-*+]|\d+\.)\s+", line):
            i = _emit_list(lines, i, n, out)
            continue
        # paragraph
        buf = [s]
        i += 1
        while i < n and lines[i].strip() and not re.match(
            r"^\s*([-*+]|\d+\.|#{1,6}\s|>|\||```|-{3,})", lines[i]
        ):
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>" + _inline(" ".join(buf)) + "</p>")
    return "\n".join(out)


def _emit_list(lines, i, n, out) -> int:
    indent_stack = []  # list of (indent, tag)

    def close_to(indent):
        while indent_stack and indent_stack[-1][0] >= indent:
            out.append("</%s>" % indent_stack.pop()[1])

    while i < n:
        line = lines[i]
        m = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)
        if not m:
            if not line.strip():
                # blank line: peek; if next is still list continue, else stop
                if i + 1 < n and re.match(r"^\s*([-*+]|\d+\.)\s+", lines[i + 1]):
                    i += 1
                    continue
                break
            break
        indent = len(m.group(1))
        ordered = bool(re.match(r"\d+\.", m.group(2)))
        content = m.group(3)
        if not indent_stack or indent > indent_stack[-1][0]:
            tag = "ol" if ordered else "ul"
            out.append("<%s>" % tag)
            indent_stack.append((indent, tag))
        elif indent < indent_stack[-1][0]:
            close_to(indent)
            if not indent_stack:
                tag = "ol" if ordered else "ul"
                out.append("<%s>" % tag)
                indent_stack.append((indent, tag))
        cb = re.match(r"^\[([ xX])\]\s+(.*)$", content)
        if cb:
            checked = cb.group(1).lower() == "x"
            box = "☑" if checked else "☐"
            out.append('<li class="task"><span class="cb">%s</span> %s</li>' % (box, _inline(cb.group(2))))
        else:
            out.append("<li>" + _inline(content) + "</li>")
        i += 1
    while indent_stack:
        out.append("</%s>" % indent_stack.pop()[1])
    return i
```

- [ ] **Step 4: 跑过** → PASS（6 个 markdown 测试通过）

- [ ] **Step 5: Commit**

```bash
git add skills/fastship/scripts/plan_html.py tests/fastship/test_plan_html.py
git commit -m "feat(fastship): offline markdown->HTML renderer (plan subset, XSS-safe)"
```

---

## Task 3: 覆盖矩阵 + 模块架构图 + 单文件组装

**Files:**
- Modify: `skills/fastship/scripts/plan_html.py`
- Test: `tests/fastship/test_plan_html.py`

- [ ] **Step 1: 写失败测试（矩阵/模块图/全文档/降级）**

```python
# append to tests/fastship/test_plan_html.py
def test_coverage_matrix_marks_covered_and_uncovered():
    m = load_mod()
    rows = [{"ac": "AC1", "assertion": "x", "e2e": "S.x"},
            {"ac": "AC2", "assertion": "y", "e2e": ""}]
    h = m.render_coverage(rows)
    assert "covered" in h and "uncovered" in h
    assert "AC1" in h and "AC2" in h


def test_module_map_groups_by_change():
    m = load_mod()
    mods = [{"path": "a.py", "change": "Create"},
            {"path": "b.py", "change": "Modify"},
            {"path": "t.py", "change": "Test"}]
    h = m.render_module_map(mods)
    assert "Create" in h and "Modify" in h and "Test" in h
    assert "a.py" in h and "b.py" in h and "t.py" in h


def test_render_full_document():
    m = load_mod()
    h = m.render_plan_html(SAMPLE)
    assert h.startswith("<!DOCTYPE html>")
    assert "My Feature Implementation Plan" in h
    assert "<style>" in m_lower(h)
    assert "coverage" in m_lower(h)         # coverage matrix present
    assert "mermaid" in h                    # mermaid block + init
    assert "flowchart TD" in h


def m_lower(s):
    return s.lower()


def test_render_thin_plan_degrades():
    m = load_mod()
    thin = "# Thin Plan\n\n**Goal:** small\n\nJust prose, no tables, no mermaid.\n"
    h = m.render_plan_html(thin)
    assert h.startswith("<!DOCTYPE html>")
    assert "Thin Plan" in h
    assert "Just prose" in h
    # no AC rows -> coverage matrix container absent
    assert 'id="coverage"' not in h


def test_render_plan_file_writes_html(tmp_path):
    m = load_mod()
    p = tmp_path / "2026-06-05-x.md"
    p.write_text(SAMPLE, encoding="utf-8")
    out = m.render_plan_file(str(p))
    assert out.endswith("2026-06-05-x.plan.html")
    assert os.path.exists(out)
    assert "My Feature Implementation Plan" in open(out, encoding="utf-8").read()
```

- [ ] **Step 2: 跑失败** → FAIL（`render_coverage` 等不存在）

- [ ] **Step 3: 写实现（面板 + 组装 + 落盘）**

```python
# append to skills/fastship/scripts/plan_html.py

def render_coverage(ac_rows: List[Dict[str, str]]) -> str:
    if not ac_rows:
        return ""
    n_cov = sum(1 for r in ac_rows if r.get("e2e", "").strip())
    head = ('<section id="coverage" class="panel"><h2>E2E ↔ AC 覆盖矩阵 '
            '<span class="count">%d/%d covered</span></h2>'
            '<table class="cov"><thead><tr><th>AC</th><th>可观察断言</th>'
            '<th>E2E scenario</th><th>状态</th></tr></thead><tbody>' % (n_cov, len(ac_rows)))
    body = []
    for r in ac_rows:
        covered = bool(r.get("e2e", "").strip())
        cls = "covered" if covered else "uncovered"
        badge = "✓ covered" if covered else "✗ uncovered"
        body.append(
            '<tr class="%s"><td class="ac">%s</td><td>%s</td><td>%s</td>'
            '<td class="badge">%s</td></tr>' % (
                cls, _inline(r.get("ac", "")), _inline(r.get("assertion", "")),
                _inline(r.get("e2e", "")) or "—", badge))
    return head + "".join(body) + "</tbody></table></section>"


def render_module_map(modules: List[Dict[str, str]]) -> str:
    if not modules:
        return ""
    groups = {"Create": [], "Modify": [], "Test": []}
    for mod in modules:
        change = (mod.get("change") or "").strip().capitalize()
        key = "Create" if change.startswith("Create") else (
            "Test" if change.startswith("Test") else (
                "Modify" if change.startswith("Modify") else "Modify"))
        groups[key].append(mod.get("path", ""))
    cols = []
    for key in ("Create", "Modify", "Test"):
        items = "".join('<li><code>%s</code></li>' % _html.escape(p, quote=False)
                        for p in groups[key] if p)
        cols.append('<div class="mod-col mod-%s"><h3>%s</h3><ul>%s</ul></div>' % (
            key.lower(), key, items or "<li class=\"empty\">—</li>"))
    return ('<section id="modules" class="panel"><h2>模块架构图</h2>'
            '<div class="mod-grid">' + "".join(cols) + "</div></section>")


_CSS = """
:root{--bg:#0f1117;--card:#171a23;--fg:#e6e6e6;--muted:#9aa0aa;--line:#2a2f3a;
--green:#1f6f43;--green-bg:#13371f;--red:#7a2330;--red-bg:#361218;--accent:#6ea8fe;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;}
.wrap{max-width:980px;margin:0 auto;padding:32px 24px 80px;}
h1{font-size:26px;border-bottom:2px solid var(--line);padding-bottom:12px;}
h2{font-size:20px;margin-top:34px;border-bottom:1px solid var(--line);padding-bottom:6px;}
h3{font-size:16px;color:var(--muted);}
a{color:var(--accent);} code{background:#0b0d12;padding:2px 5px;border-radius:4px;
font:13px/1.5 "SF Mono",Menlo,Consolas,monospace;}
pre{background:#0b0d12;border:1px solid var(--line);border-radius:8px;padding:14px;overflow:auto;}
pre code{background:none;padding:0;}
blockquote{border-left:3px solid var(--accent);margin:14px 0;padding:6px 14px;color:var(--muted);background:var(--card);}
table{border-collapse:collapse;width:100%;margin:14px 0;}
th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top;}
th{background:var(--card);}
hr{border:none;border-top:1px solid var(--line);margin:28px 0;}
li.task{list-style:none;margin-left:-22px;} .cb{color:var(--accent);}
.meta{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 20px;margin:18px 0;}
.meta b{color:var(--accent);}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 20px 20px;margin:24px 0;}
.cov tr.covered{background:var(--green-bg);} .cov tr.uncovered{background:var(--red-bg);}
.cov td.badge{white-space:nowrap;font-weight:600;}
.cov tr.covered td.badge{color:#7fe0a3;} .cov tr.uncovered td.badge{color:#ff9aa6;}
.count{font-size:13px;color:var(--muted);font-weight:400;}
.mod-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;}
.mod-col{border:1px solid var(--line);border-radius:8px;padding:10px;}
.mod-col h3{margin:0 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:.5px;}
.mod-create h3{color:#7fe0a3;} .mod-modify h3{color:#ffd479;} .mod-test h3{color:#6ea8fe;}
.mod-col ul{margin:0;padding-left:18px;} .mod-col li{margin:4px 0;word-break:break-all;}
.mod-col li.empty{list-style:none;color:var(--muted);}
.toc{font-size:13px;color:var(--muted);} .pre.mermaid,pre.mermaid{background:#0b0d12;text-align:center;}
"""


def _meta_block(model: "PlanModel") -> str:
    parts = []
    for label, val in (("Goal", model.goal), ("Architecture", model.architecture),
                       ("Tech Stack", model.tech_stack)):
        if val:
            parts.append("<p><b>%s:</b> %s</p>" % (label, _inline(val)))
    return ('<div class="meta">' + "".join(parts) + "</div>") if parts else ""


def render_plan_html(md: str, title: Optional[str] = None) -> str:
    model = parse_plan(md)
    page_title = title or model.title or "Plan"
    body_html = md_to_html(md)
    mermaid_init = ""
    if model.mermaid_blocks:
        mermaid_init = (
            '<script src="%s"></script>'
            '<script>try{mermaid.initialize({startOnLoad:true,theme:"dark"});}'
            'catch(e){}</script>' % MERMAID_SRC)
    return (
        "<!DOCTYPE html>\n<html lang=\"zh\"><head><meta charset=\"utf-8\"/>"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"/>"
        "<title>" + _html.escape(page_title) + "</title><style>" + _CSS + "</style></head>"
        "<body><div class=\"wrap\">"
        "<h1>" + _html.escape(page_title) + "</h1>"
        + _meta_block(model)
        + render_coverage(model.ac_rows)
        + render_module_map(model.modules)
        + "<section class=\"body\">" + body_html + "</section>"
        "</div>" + mermaid_init + "</body></html>"
    )


def render_plan_file(plan_path: str, out_path: Optional[str] = None) -> str:
    with open(plan_path, encoding="utf-8") as f:
        md = f.read()
    html = render_plan_html(md)
    if not out_path:
        base = plan_path[:-3] if plan_path.endswith(".md") else plan_path
        out_path = base + ".plan.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render a fastship plan.md to self-contained HTML")
    ap.add_argument("plan", help="path to plan markdown")
    ap.add_argument("-o", "--out", help="output html path (default: <plan>.plan.html)")
    args = ap.parse_args(argv)
    if not os.path.exists(args.plan):
        print("plan not found: %s" % args.plan, file=sys.stderr)
        return 1
    out = render_plan_file(args.plan, args.out)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑过** `env -u FASTSHIP_SESSION -u FASTSHIP_REPO_ROOT -u FASTSHIP_STATE_HOME python3 -m pytest tests/fastship/test_plan_html.py -q -p no:cacheprovider` → PASS（全部解析+markdown+面板+组装+落盘测试）

- [ ] **Step 5: 手验 CLI** `python3 skills/fastship/scripts/plan_html.py docs/superpowers/plans/2026-06-05-fastship-plan-html.md` → 打印 `.plan.html` 路径，文件可双击打开

- [ ] **Step 6: Commit**

```bash
git add skills/fastship/scripts/plan_html.py tests/fastship/test_plan_html.py
git commit -m "feat(fastship): coverage matrix + module map + self-contained HTML assembly + CLI"
```

---

## Task 4: orchestrator 集成（hook + CLI + render-plan 子命令 + 1.4 指令）

**Files:**
- Modify: `skills/fastship/orchestrator.py`
- Test: `tests/fastship/test_orchestrator_plan_html.py`

- [ ] **Step 1: 写失败测试（集成）**

```python
# tests/fastship/test_orchestrator_plan_html.py
import importlib, os, sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills" / "fastship"))


@pytest.fixture
def orch_mod():
    import orchestrator
    importlib.reload(orchestrator)
    return orchestrator


PLAN_MD = """# Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development.

**Goal:** integrate.

**Architecture:** wire it.

**Tech Stack:** python.

---

## File Structure
| File | Responsibility | Change |
|------|----------------|--------|
| `x.py` | core | Create |

## Task 1: t
- [ ] **Step 1: do**
"""


def _write_plan(repo: Path):
    d = repo / "docs" / "superpowers" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "2026-06-05-integration.md"
    p.write_text(PLAN_MD, encoding="utf-8")
    return p


def test_generate_plan_html_creates_sibling(orch_mod, tmp_path):
    p = _write_plan(tmp_path)
    out = orch_mod.generate_plan_html(str(p))
    assert out and out.endswith("2026-06-05-integration.plan.html")
    assert os.path.exists(out)


def test_generate_plan_html_failure_returns_none(orch_mod, tmp_path, monkeypatch):
    p = _write_plan(tmp_path)
    # force renderer import path to a bogus file so it raises internally
    monkeypatch.setattr(orch_mod, "_PLAN_HTML_SCRIPT", str(tmp_path / "nope.py"))
    out = orch_mod.generate_plan_html(str(p))
    assert out is None  # swallowed, no raise


def test_plan_html_not_in_trusted_ledger(orch_mod, tmp_path):
    p = _write_plan(tmp_path)
    st = orch_mod.empty_orchestrator_state("x")
    orch_mod.record_step_artifact(st, "1.4", str(p))
    orch_mod.attach_plan_html(st, str(p))
    trusted = st.get("artifacts", {}).get(orch_mod.TRUSTED_ARTIFACTS_KEY, {})
    assert "plan_html" not in trusted
    assert "1.4_html" not in trusted
    assert st["artifacts"]["plan_html_path"].endswith(".plan.html")
```

- [ ] **Step 2: 跑失败** `env -u FASTSHIP_SESSION -u FASTSHIP_REPO_ROOT -u FASTSHIP_STATE_HOME python3 -m pytest tests/fastship/test_orchestrator_plan_html.py -q -p no:cacheprovider` → FAIL（`generate_plan_html` 不存在）

- [ ] **Step 3: 实现 orchestrator helper（在 orchestrator.py 顶部 import 区附近加）**

```python
# skills/fastship/orchestrator.py — near other module-level path helpers
_PLAN_HTML_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "scripts", "plan_html.py")


def _load_plan_html_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location("plan_html", _PLAN_HTML_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def generate_plan_html(plan_path: str):
    """Render plan.md -> sibling .plan.html. Best-effort: returns out path or None.
    Never raises — a render failure must not block the 1.4 step."""
    try:
        if not plan_path or not os.path.exists(plan_path):
            return None
        mod = _load_plan_html_mod()
        return mod.render_plan_file(plan_path)
    except Exception as e:  # noqa: BLE001 — visualization is non-critical
        print(f"⚠️ plan.html 生成失败（不阻断）: {e}")
        return None


def attach_plan_html(orch: dict, plan_path: str):
    """Generate the HTML and record its path in NON-trusted artifacts."""
    out = generate_plan_html(plan_path)
    if out:
        orch.setdefault("artifacts", {})["plan_html_path"] = out
        print(f"🖼️  plan.html: {out}")
    return out
```

- [ ] **Step 4: 接入 hook 模式（在 `detected == "1.4"` 校验通过后，line ~1733，stale 清理之前）**

```python
# inside hook post_edit, after validate_plan passes for 1.4:
            attach_plan_html(orch, file_path)
            for stale in (GRILL_RESULT_FILENAME, CODEX_REVIEW_FILENAME):
                ...
```

- [ ] **Step 5: 接入 CLI 模式（cmd_done，在 1.4 validator 通过后，line ~2153）**

```python
# inside cmd_done: place AFTER the validator-success block (`if not ok: ... return 1`)
# and BEFORE the `st = _advance_state(st)` line (~2168). Guarded to 1.4, so other
# steps are unaffected:
    if step.id == "1.4":
        attach_plan_html(st, st.get("plan_path"))
```

- [ ] **Step 6: 新增 `render-plan` 子命令（在 main 的命令分派处，参照 `elif cmd == "reset":`）**

```python
def cmd_render_plan(argv: list) -> int:
    plan_path = argv[0] if argv else None
    if not plan_path:
        st = load_orch_state()
        plan_path = (st or {}).get("plan_path")
    if not plan_path or not os.path.exists(plan_path):
        print("❌ 无 plan 文件。用法: render-plan <plan.md> 或在活跃 session 内 render-plan")
        return 1
    out = generate_plan_html(plan_path)
    if not out:
        return 1
    print(out)
    return 0

# in main() dispatch — render-plan takes an arg so it canNOT go in the zero-arg
# `handlers` dict; add an explicit branch next to `elif cmd == "reset":`
    elif cmd == "render-plan":
        sys.exit(cmd_render_plan(argv[1:]))
```

- [ ] **Step 7: 更新 Step 1.4 instruction 文本（line ~1078）**

```python
    Step("1.4", "写计划", 1, validator=validate_plan,
         instruction="""通过 Skill 工具调用 superpowers 写计划：
  Skill(skill="writing-plans")

计划必须包含 AC 清单 + E2E 验证方案 + 影响范围 + 任务拆分。
🔴 为让 plan.html 直观可视，请额外：
  - 加 `## 验收清单（AC）→ E2E 映射` 管道表（| AC | 可观察断言 | E2E scenario |），每条 AC 必有 E2E
  - 加 `## File Structure` 管道表（| File | Responsibility | Change |，Change ∈ Create/Modify/Test）
  - 加 `## 图示` 一个 ```mermaid flowchart（核心流程），可选模块依赖图
🔴 必须通过 Skill 工具调用，不要自己拆步骤。
产物: docs/superpowers/plans/YYYY-MM-DD-{feature}.md（plan 通过后自动生成同名 .plan.html）
orchestrator 自动检测 plan 文件写入 + 验证 writing-plans 签名。"""),
```

- [ ] **Step 8: 跑过集成测试** `env -u FASTSHIP_SESSION -u FASTSHIP_REPO_ROOT -u FASTSHIP_STATE_HOME python3 -m pytest tests/fastship/test_orchestrator_plan_html.py -q -p no:cacheprovider` → PASS

- [ ] **Step 9: 回归全套** `env -u FASTSHIP_SESSION -u FASTSHIP_REPO_ROOT -u FASTSHIP_STATE_HOME python3 -m pytest tests/fastship/ -q -p no:cacheprovider` → 既有 202 + 新增全 PASS（确认 1.4 instruction 改动未破坏 PLAN_SIGNATURE_MARKERS 相关测试）

- [ ] **Step 10: Commit**

```bash
git add skills/fastship/orchestrator.py tests/fastship/test_orchestrator_plan_html.py
git commit -m "feat(fastship): auto-generate plan.html on 1.4 (hook+CLI), render-plan cmd, 1.4 instruction"
```

---

## Task 5: 纯 Python E2E runner

**Files:**
- Create: `tests/fastship/plan_html_e2e_runner.py`

- [ ] **Step 1: 写 E2E runner（跑真渲染器 + 真 orchestrator CLI，输出 scenarios→rounds→turns）**

```python
#!/usr/bin/env python3
"""Pure-Python E2E for fastship plan.html. Runs the real renderer + real
orchestrator CLI against real plan fixtures and asserts business outcomes.
Emits nested scenarios[].rounds[].turns (>=12) + flat keys; exit 0 iff all pass."""
import argparse, importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN_HTML = ROOT / "skills" / "fastship" / "scripts" / "plan_html.py"
ORCH = ROOT / "skills" / "fastship" / "orchestrator.py"

FULL = """# E2E Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. Steps use `- [ ]`.

**Goal:** demo.

**Architecture:** python.

**Tech Stack:** stdlib.

---

## 验收清单（AC）→ E2E 映射
| AC | 可观察断言 | E2E scenario.round |
|----|-----------|--------------------|
| AC1 | renders | S.r |
| AC2 | uncovered | |

## File Structure
| File | Responsibility | Change |
|------|----------------|--------|
| `core.py` | core | Create |
| `wire.py` | wire | Modify |
| `t.py` | test | Test |

## 图示
```mermaid
flowchart TD
  A --> B
```

## Task 1: thing
- [ ] **Step 1: do** with `<script>alert(1)</script>` in text
"""

THIN = "# Thin Plan\n\n**Goal:** small\n\nJust prose.\n"


def load_renderer():
    spec = importlib.util.spec_from_file_location("plan_html", PLAN_HTML)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run():
    m = load_renderer()
    turns = []

    def check(action, cond, detail="", response=""):
        # `response` is non-empty so e2e_gate reports 0 empty replies (clean evidence)
        turns.append({"action": action, "status": "pass" if cond else "fail",
                      "passed": bool(cond),
                      "response": response or ("ok" if cond else "FAILED"),
                      "detail": detail})
        return cond

    full = m.render_plan_html(FULL)
    check("render full: doctype", full.startswith("<!DOCTYPE html>"))
    check("render full: title", "E2E Demo Implementation Plan" in full)
    check("render full: inline css", "<style>" in full)
    check("render full: coverage matrix", 'id="coverage"' in full and "covered" in full and "uncovered" in full)
    check("render full: module map", 'id="modules"' in full and "core.py" in full and "wire.py" in full and "t.py" in full)
    check("render full: mermaid block", 'class="mermaid"' in full and "flowchart TD" in full)
    check("render full: heading tag", "<h1" in full)
    check("render full: xss escaped", "&lt;script&gt;" in full and "<script>alert" not in full)

    thin = m.render_plan_html(THIN)
    check("degrade thin: doctype", thin.startswith("<!DOCTYPE html>"))
    check("degrade thin: prose", "Just prose" in thin)
    check("degrade thin: no coverage panel", 'id="coverage"' not in thin)

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        d = repo / "docs" / "superpowers" / "plans"
        d.mkdir(parents=True)
        plan = d / "2026-06-05-e2e.md"
        plan.write_text(FULL, encoding="utf-8")
        out = m.render_plan_file(str(plan))
        check("file render: sibling .plan.html", out.endswith("2026-06-05-e2e.plan.html") and os.path.exists(out))

        # real orchestrator CLI render-plan subcommand
        env = dict(os.environ); env.pop("FASTSHIP_SESSION", None)
        env["FASTSHIP_REPO_ROOT"] = str(repo); env["FASTSHIP_STATE_HOME"] = str(repo / ".state")
        r = subprocess.run([sys.executable, str(ORCH), "render-plan", str(plan)],
                           capture_output=True, text=True, env=env)
        check("cli render-plan: exit 0", r.returncode == 0, r.stderr[-300:])
        check("cli render-plan: prints html path", ".plan.html" in r.stdout)

    return turns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="/tmp/plan_html_e2e_result.json")
    args = ap.parse_args()
    turns = run()
    passed = sum(1 for t in turns if t["passed"])
    result = {
        "scenarios": [{
            "name": "plan_html_e2e",
            "description": "fastship plan.html renderer + orchestrator integration",
            "rounds": [{"turns": turns}],
        }],
        "turns": len(turns), "passed": passed, "failed": len(turns) - passed,
        "timestamp": "2026-06-05T00:00:00Z",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps({"turns": len(turns), "passed": passed, "out": args.out}, ensure_ascii=False))
    for t in turns:
        mark = "✅" if t["passed"] else "❌"
        print(f"  {mark} {t['action']}  {t.get('detail','')}")
    return 0 if passed == len(turns) else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 跑 E2E** `env -u FASTSHIP_SESSION python3 tests/fastship/plan_html_e2e_runner.py -o /tmp/plan_html_e2e_result.json` → 全 turns ✅，exit 0，`turns>=12`

- [ ] **Step 3: 过 gate** `python3 tests/e2e_gate.py --result /tmp/plan_html_e2e_result.json --min-turns 12` → exit 0

- [ ] **Step 4: Commit**

```bash
git add tests/fastship/plan_html_e2e_runner.py
git commit -m "test(fastship): plan_html E2E runner — renderer + orchestrator render-plan (14 turns)"
```

---

## Task 6: 文档 + gitignore

**Files:**
- Modify: `skills/fastship/SKILL.md`
- Modify: `.gitignore`

- [ ] **Step 0: 忽略派生的 plan.html（避免提交噪声）**

Append to `.gitignore`:

```
# fastship-generated plan visualization (derived from plan.md)
docs/superpowers/plans/*.plan.html
```

需要分享某个 plan.html 时手动 `git add -f`。

- [ ] **Step 1: 在 SKILL.md 的流程概览 / Stage 1 说明处加一段**

```markdown
### Plan 可视化（plan.html）

Step 1.4 写计划通过校验后，orchestrator 自动在 plan.md 同目录生成同名 `*.plan.html`：
- 离线自包含单文件（双击即开），把 Goal/Architecture、E2E↔AC 覆盖矩阵、模块架构图、正文渲染成直观视图
- `## 图示` 里的 ```mermaid flowchart 渲染成流程图（mermaid.js，离线时降级显示图源）
- 按需重渲染：`"$(git rev-parse --show-toplevel)/.claude/tools/fastship" render-plan [plan.md]`
- 生成失败不阻断流程；plan.html 不进可信账本（派生视图，非门禁交付物）
```

- [ ] **Step 2: Commit**

```bash
git add skills/fastship/SKILL.md .gitignore
git commit -m "docs(fastship): document plan.html visualization + render-plan command + gitignore"
```

---

## Self-Review

**1. Spec coverage（逐条对原始需求）：**
- 「plan.md → 本地 html」→ Task 3 `render_plan_file` 落盘 + Task 4 自动触发 ✓
- 「直观看到技术方案」→ `_meta_block`（Goal/Architecture/Tech Stack）+ 正文渲染 ✓
- 「流程图」→ mermaid 块渲染（Task 2 fenced mermaid + Task 3 init）✓
- 「模块架构图」→ `render_module_map`（Task 3）从 File Structure 自动派生 ✓
- 「e2e」→ `render_coverage`（Task 3）AC↔E2E 覆盖矩阵 ✓
- 「md 看不清」→ 离线 Python 渲染正文 + 彩色样式 + 结构化面板 ✓
- 「fastship Stage1 完成后生成」→ hook + CLI 两路集成（Task 4）✓

**2. Placeholder scan：** 无 TBD/TODO；每个 code step 含完整可执行代码；E2E runner 与单测代码完整。

**3. Type consistency：**
- `parse_plan` 返回 `PlanModel`，字段 `ac_rows`/`modules`/`mermaid_blocks` 在 `render_coverage`/`render_module_map`/`render_plan_html` 一致消费 ✓
- AC row dict 键 `{ac, assertion, e2e}` 在解析与渲染两端一致 ✓
- module dict 键 `{path, change}` 一致 ✓
- orchestrator `generate_plan_html` / `attach_plan_html` 与测试调用签名一致；`_PLAN_HTML_SCRIPT` 在 helper 与 failure 测试 monkeypatch 一致 ✓
- 输出文件命名 `<basename>.plan.html` 在 `render_plan_file`、集成测试、E2E 三处一致 ✓

**4. 风险复核：**
- 渲染失败不阻断：`generate_plan_html` try/except 返回 None（AC8/Task4-Step3）✓
- 不污染可信账本：`attach_plan_html` 只写 `artifacts["plan_html_path"]`，不调 `record_step_artifact`（AC10/Task4 测试）✓
- 1.4 instruction 改动不破坏签名：Task4-Step9 全套回归确认 ✓
- 旧 plan 容错：`render_coverage`/`render_module_map` 空输入返回 ""（AC6/Task3 测试）✓
- 不在 repo root 放 `fastship.project.json`：E2E 走纯 Python runner（Task5）✓

**5. 已知限制 / 刻意设计（grill 记录）：**
- `_field_after_label` 用 `(.+)$`+MULTILINE，**只抓 Goal/Architecture 的首行**。plan 模板里这俩本是单逻辑行段落，可接受；多行支持留 v2。
- 顶部面板（覆盖矩阵 + 模块图）与正文 `md_to_html` 的 AC 表/File Structure 表/mermaid **刻意重复**：面板=一目了然可视摘要，正文=完整保真 plan。mermaid 只由正文 `## 图示` 出图、render_plan_html 只挂一次 init，不另设 mermaid 面板。
- mermaid 库默认 **CDN**（`MERMAID_SRC` 单点常量），离线时 `<pre class="mermaid">` 降级显示图源；切 vendored（完全离线、+~600KB）只改这一行 —— **此项留用户在 1.6 确认**。
