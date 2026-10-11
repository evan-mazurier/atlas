"""Generate tools/atlas-v2.ico, the desktop-shortcut icon, from cockpit/web2/favicon.svg.

One source of truth for the icon: the SVG the web app uses as its tab and taskbar icon. Renders
each Windows size (16 to 256 px) with headless Chromium (Playwright) and packs them into one .ico.
Usage: py -3.12 tools/make_cockpit_icon.py
"""
import io
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "cockpit" / "web2" / "favicon.svg"
DEST = Path(__file__).with_name("atlas-v2.ico")
SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> None:
    svg = SVG.read_text(encoding="utf-8")
    imgs = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        for s in SIZES:
            pg.set_viewport_size({"width": s, "height": s})
            sized = svg.replace("<svg ", f'<svg width="{s}" height="{s}" ', 1)
            pg.set_content(f'<html><body style="margin:0;background:transparent">{sized}</body></html>')
            imgs.append(Image.open(io.BytesIO(pg.screenshot(omit_background=True))).convert("RGBA"))
        b.close()
    imgs[-1].save(DEST, sizes=[i.size for i in imgs], append_images=imgs[:-1])
    print(f"wrote {DEST}")


if __name__ == "__main__":
    main()
