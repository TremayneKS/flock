"""Draw web/icon.ico (the desktop shortcut's icon) from the same shapes as web/icon.svg: a dark rounded square, an orange
prompt chevron and a light underline. Needs Pillow (pip install pillow): python tools/make_icon.py"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "web" / "icon.ico"
S = 1024                       # drawn big, then scaled down for smooth edges
k = S / 32                     # the SVG's 32x32 grid


def line(d, pts, colour, width):
    pts = [(x * k, y * k) for x, y in pts]
    d.line(pts, fill=colour, width=int(width * k), joint="curve")
    r = width * k / 2
    for x, y in pts:           # round caps
        d.ellipse((x - r, y - r, x + r, y + r), fill=colour)


img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((0, 0, S - 1, S - 1), radius=int(7 * k), fill="#1c1c1a")
line(d, [(8, 10), (14, 16), (8, 22)], "#ff8a3d", 2.6)
line(d, [(16.5, 22), (24, 22)], "#f3f2ef", 2.6)
sizes = [16, 24, 32, 48, 64, 128, 256]
img.resize((256, 256), Image.LANCZOS).save(OUT, sizes=[(s, s) for s in sizes])
print("wrote", OUT)
