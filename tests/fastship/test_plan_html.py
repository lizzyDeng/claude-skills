import importlib.util
import os
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


# ── Task 1: parse layer ──

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


# ── Task 2: markdown → HTML ──

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


def test_md_xss_fenced_info_string_escaped():
    m = load_mod()
    payload = 'python"></code></pre><script>alert(1)</script>'
    h = m.md_to_html("```" + payload + "\nx=1\n```\n")
    assert "<script>alert(1)</script>" not in h  # info string can't break out of class attr


def test_md_xss_link_href_no_breakout():
    m = load_mod()
    h = m.md_to_html('[a](http://x?q=") autofocus onfocus=alert(1) x=)\n')
    # the `"` in the URL must be neutralized so it can't break out of href=""
    assert "onfocus=alert" not in h or "&quot;" in h
    assert 'href="http://x?q="' not in h  # no premature attribute close


# ── Task 3: panels + assembly ──

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


def m_lower(s):
    return s.lower()


def test_render_full_document():
    m = load_mod()
    h = m.render_plan_html(SAMPLE)
    assert h.startswith("<!DOCTYPE html>")
    assert "My Feature Implementation Plan" in h
    assert "<style>" in m_lower(h)
    assert "coverage" in m_lower(h)         # coverage matrix present
    assert "mermaid" in h                    # mermaid block + init
    assert "flowchart TD" in h


def test_render_thin_plan_degrades():
    m = load_mod()
    thin = "# Thin Plan\n\n**Goal:** small\n\nJust prose, no tables, no mermaid.\n"
    h = m.render_plan_html(thin)
    assert h.startswith("<!DOCTYPE html>")
    assert "Thin Plan" in h
    assert "Just prose" in h
    assert 'id="coverage"' not in h


def test_render_plan_file_writes_html(tmp_path):
    m = load_mod()
    p = tmp_path / "2026-06-05-x.md"
    p.write_text(SAMPLE, encoding="utf-8")
    out = m.render_plan_file(str(p))
    assert out.endswith("2026-06-05-x.plan.html")
    assert os.path.exists(out)
    assert "My Feature Implementation Plan" in open(out, encoding="utf-8").read()
