#!/usr/bin/env python3
"""把 poster.html 变成**自包含单文件** —— 内联 CSS + base64 内嵌字体。

用法：  python3 bundle.py <poster.html> [-o out.html]

不带 -o 时**原地覆盖**（幂等：重复跑不会重复内联）。

为什么必须做这一步：
  海报靠 `<link href="./dayflow.css">` 找样式，css 又靠 `url('./fonts/...')` 找得意黑。
  单独把 html 发给别人 / 丢进 artifact，这两跳全断——样式没了、标题回落系统黑体，
  画风就没了，但**页面照样能打开**，所以这个失败是静默的。自包含之后单文件即完整。
"""
import base64
import re
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent
MARK = "poster-inline-css"          # 幂等标记：认这个 id 就说明已经内联过

# css 里可能出现的字体引用 → (相对路径, MIME)
FONT_MIME = {".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf"}


def inline_fonts(css: str, css_dir: Path) -> tuple[str, int]:
    """把 css 里的 url('./fonts/x.woff2') 换成 base64 data URI。返回 (css, 内嵌数)。"""
    n = 0

    def sub(m: re.Match) -> str:
        nonlocal n
        raw = m.group("path").strip("'\"")
        if raw.startswith("data:"):
            return m.group(0)
        target = (css_dir / raw).resolve()
        mime = FONT_MIME.get(target.suffix.lower())
        if mime is None or not target.is_file():
            return m.group(0)           # 不是字体、或找不到 → 原样留着（如内联 svg）
        b64 = base64.b64encode(target.read_bytes()).decode("ascii")
        n += 1
        return f"url(data:{mime};base64,{b64})"

    # 只吃 url(...)，不碰 filter: url(#wob-1) 这种同文档片段引用
    css = re.sub(r"url\(\s*(?P<path>(?!#)[^)]+?)\s*\)", sub, css)
    return css, n


def bundle(src: Path, out: Path) -> None:
    html = src.read_text(encoding="utf-8")

    # 幂等：先把上一次内联进去的 <style id=poster-inline-css> 整块摘掉，
    # 换回一个占位 link，后面统一重新内联。
    html, undone = re.subn(
        rf'<style id="{MARK}">.*?</style>',
        '<link rel="stylesheet" href="./dayflow.css">',
        html,
        flags=re.S,
    )

    m = re.search(r'<link[^>]+rel=["\']stylesheet["\'][^>]*href=["\'](?P<href>[^"\']+)["\'][^>]*>', html)
    if not m:
        sys.exit(f"✗ {src}: 找不到 <link rel=stylesheet>，不知道要内联哪个 css")

    href = m.group("href")
    # css 优先从 html 旁边找（可能被 render.py 拷过去了），否则回落 kit
    cand = (src.resolve().parent / href).resolve()
    css_path = cand if cand.is_file() else (KIT / Path(href).name)
    if not css_path.is_file():
        sys.exit(f"✗ 找不到 css：{cand} 也不在 {KIT}")

    css, n_font = inline_fonts(css_path.read_text(encoding="utf-8"), css_path.parent)
    html = html[: m.start()] + f'<style id="{MARK}">\n{css}\n</style>' + html[m.end():]

    out.write_text(html, encoding="utf-8")
    size_mb = out.stat().st_size / 1024 / 1024
    again = "（重新内联）" if undone else ""
    print(f"✓ {out}  {size_mb:.2f} MB  内嵌字体 {n_font} 个{again}")
    if n_font == 0:
        print("  ⚠ 没有内嵌到任何字体 —— 标题会回落系统黑体，检查 fonts/ 是否在 css 旁边")


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        sys.exit(__doc__)
    out = None
    if "-o" in argv:
        i = argv.index("-o")
        out = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    src = Path(argv[0])
    if not src.is_file():
        sys.exit(f"✗ 没有这个文件：{src}")
    bundle(src, out or src)
