#!/usr/bin/env python3
"""把 poster.html 渲染成一张长图。

用法：  python3 render.py [poster.html] [out.png] [--scale 2]

做两件事：
  1. 用系统 Chrome 无头渲染（窗口开得足够高）
  2. 用 Pillow 把底部多余的「桌面底色」裁掉 —— Chrome 新版无头不支持
     整页截图，只能开高窗口再裁
"""
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROBE_HEIGHT = 6000   # 先开这么高，再裁
WIDTH = 1024


def ensure_assets(html: Path) -> None:
    """把 dayflow.css + fonts/ 补到 html 旁边。

    🔴 忘了拷 fonts/ 是最隐蔽的坑：标题会静默回落成系统黑体，图还能出，
    但画风就没了。所以这里做成自动的，不靠人记。
    """
    kit = Path(__file__).resolve().parent
    dst = html.resolve().parent
    if dst == kit:
        return
    css = dst / "dayflow.css"
    if not css.exists():
        shutil.copy2(kit / "dayflow.css", css)
    fonts = dst / "fonts"
    if not fonts.exists():
        shutil.copytree(kit / "fonts", fonts)


def render(html: Path, out: Path, scale: int = 2) -> None:
    ensure_assets(html)
    raw = out.with_suffix(".raw.png")
    subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-sandbox",
            "--virtual-time-budget=5000",
            f"--window-size={WIDTH},{PROBE_HEIGHT}",
            f"--force-device-scale-factor={scale}",
            f"--screenshot={raw}",
            html.resolve().as_uri(),
        ],
        check=True,
        capture_output=True,
    )

    img = Image.open(raw).convert("RGB")
    w, h = img.size
    # 空白色 = 最后一行的颜色。不能取左上角 —— poster 满宽时两侧没有桌面色露出来。
    px = img.load()
    desk = px[w // 2, h - 1]

    def row_is_desk(y: int) -> bool:
        # 只抽查中轴附近若干点，比整行扫快得多
        return all(px[x, y] == desk for x in range(w // 4, 3 * w // 4, 37))

    bottom = h - 1
    while bottom > 0 and row_is_desk(bottom):
        bottom -= 1

    img.crop((0, 0, w, min(bottom + 1 + 2 * scale, h))).save(out)
    raw.unlink(missing_ok=True)
    print(f"{out}  {img.crop((0, 0, w, bottom + 1)).size[0]}x{bottom + 1}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    scale = 2
    if "--scale" in sys.argv:
        scale = int(sys.argv[sys.argv.index("--scale") + 1])
    src = Path(args[0]) if args else Path("poster.html")
    dst = Path(args[1]) if len(args) > 1 else src.with_suffix(".png")
    render(src, dst, scale)
