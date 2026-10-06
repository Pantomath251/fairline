"""1024x1024 market cover images (Panta requires a public square image for every new market)."""
from __future__ import annotations

import io
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

W = H = 1024
BG_TOP, BG_BOTTOM = (11, 18, 32), (22, 44, 78)
ACCENT = (64, 220, 160)
TEXT, MUTED = (240, 244, 250), (150, 166, 190)


def _font(size: int, bold: bool = False):
    for name in (("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else
                 ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _fit(draw: ImageDraw.ImageDraw, text: str, max_w: int, size: int, bold: bool = True):
    while size > 28:
        f = _font(size, bold)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return _font(size, bold)


def _center(draw, y, text, font, fill):
    w = draw.textlength(text, font=font)
    draw.text(((W - w) / 2, y), text, font=font, fill=fill)


def render(fx: dict, question: str, fair_yes: float | None) -> bytes:
    img = Image.new("RGB", (W, H), BG_TOP)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)))
    d.rectangle([0, 0, W, 10], fill=ACCENT)

    comp = (fx.get("tournament") or fx.get("sport", "")).upper()
    _center(d, 70, comp[:48], _fit(d, comp[:48], 900, 40, False), MUTED)
    ko = datetime.fromtimestamp(int(fx["startTimestamp"]), timezone.utc).strftime("%a %d %b %Y · %H:%M UTC")
    _center(d, 125, ko, _font(34), MUTED)

    home, away = fx["home"], fx["away"]
    _center(d, 270, home, _fit(d, home, 900, 96), TEXT)
    _center(d, 400, "vs", _font(48), ACCENT)
    _center(d, 470, away, _fit(d, away, 900, 96), TEXT)

    q = question if len(question) <= 60 else question[:57] + "..."
    _center(d, 640, q, _fit(d, q, 920, 44, False), TEXT)

    if fair_yes is not None:
        pct = f"Fair YES {fair_yes * 100:.1f}%"
        f = _font(64, True)
        w = d.textlength(pct, font=f)
        x0, y0 = (W - w) / 2 - 36, 735
        d.rounded_rectangle([x0, y0, x0 + w + 72, y0 + 104], radius=24, outline=ACCENT, width=4)
        d.text(((W - w) / 2, y0 + 16), pct, font=f, fill=ACCENT)

    _center(d, 905, "Fairline  ·  Powered by Panta", _font(36, True), MUTED)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
