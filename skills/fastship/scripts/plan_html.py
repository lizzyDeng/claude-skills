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
    in_fence = False
    for line in md.split("\n"):
        s = line.strip()
        if s.startswith("```"):
            # Fenced code blocks may contain EXAMPLE markdown tables (esp. in
            # meta-plans). They are not real structure — skip their interior.
            in_fence = not in_fence
            if cur:
                tables.append(cur)
                cur = []
            continue
        if in_fence:
            continue
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


# ───────────────────────────── markdown → HTML (offline subset) ──────────────────────────

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

    def _link(mm):
        # `out` is already escaped with quote=False, so only the attribute-breakout
        # char `"` remains; neutralize it so a crafted URL can't escape the href.
        return '<a href="%s">%s</a>' % (mm.group(2).replace('"', "&quot;"), mm.group(1))

    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link, out)
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
                cls = (' class="language-%s"' % _html.escape(info, quote=True)) if info else ""
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


# ───────────────────────────── structured panels + assembly ──────────────────────────

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
.toc{font-size:13px;color:var(--muted);} pre.mermaid{background:#0b0d12;text-align:center;}
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
