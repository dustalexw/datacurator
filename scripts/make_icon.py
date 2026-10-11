"""Renders the Data Curator app icon (deep-space iridescent orbits around a glass data stack)
and the README header. Output: assets/AppIcon.png (1024), .icns, .ico, and a PNG set for Qt.

usage: python scripts/make_icon.py
"""
from __future__ import annotations

import math
import os
import random

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "src", "datacurator", "gui", "resources")
S = 2048  # supersampled canvas
rng = random.Random(7)
nrng = np.random.default_rng(7)

PALETTE = [(62, 232, 255), (77, 120, 255), (150, 80, 255), (236, 72, 216), (255, 120, 120), (255, 176, 87),
           (62, 232, 255)]


def pal(t: float) -> tuple[int, int, int]:
    t = t % 1.0
    x = t * (len(PALETTE) - 1)
    i = int(x)
    f = x - i
    a, b = PALETTE[i], PALETTE[i + 1]
    return tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))


def arr(im: Image.Image) -> np.ndarray:
    return np.asarray(im, dtype=np.float32) / 255.0


def img(a: np.ndarray, mode="RGBA") -> Image.Image:
    return Image.fromarray(np.clip(a * 255 + 0.5, 0, 255).astype(np.uint8), mode)


def over(dst: np.ndarray, src: np.ndarray) -> np.ndarray:
    """Premultiplied-free alpha over for RGBA float arrays."""
    sa = src[..., 3:4]
    out = dst.copy()
    out[..., :3] = src[..., :3] * sa + dst[..., :3] * (1 - sa)
    out[..., 3:4] = sa + dst[..., 3:4] * (1 - sa)
    return out


def add_glow(dst: np.ndarray, layer: Image.Image, radius: float, strength: float) -> np.ndarray:
    g = arr(layer.filter(ImageFilter.GaussianBlur(radius)))
    dst[..., :3] += g[..., :3] * g[..., 3:4] * strength
    return dst


def rounded_mask(size: int, inset: float, radius: float) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    p = int(size * inset)
    d.rounded_rectangle([p, p, size - p, size - p], radius=int(size * radius), fill=255)
    return m


# ------------------------------------------------------------------ background

def background() -> np.ndarray:
    y, x = np.mgrid[0:S, 0:S].astype(np.float32) / S
    base = np.zeros((S, S, 4), np.float32)
    top = np.array([0.07, 0.05, 0.22])
    bot = np.array([0.16, 0.04, 0.26])
    t = (x * 0.45 + y * 0.55)[..., None]
    base[..., :3] = top * (1 - t) + bot * t
    r = np.sqrt((x - 0.5) ** 2 + (y - 0.48) ** 2)[..., None]
    base[..., :3] += np.array([0.10, 0.12, 0.38]) * np.clip(1 - r / 0.55, 0, 1) ** 2
    # nebula: multi-octave smooth noise tinted magenta/blue
    neb = np.zeros((S, S), np.float32)
    for octave, w in ((6, 0.55), (12, 0.3), (24, 0.15)):
        n = Image.fromarray((nrng.random((octave, octave)) * 255).astype(np.uint8)).resize((S, S), Image.BICUBIC)
        neb += arr(n) * w
    neb = np.clip((neb - 0.45) * 2.2, 0, 1)
    tint = nrng.random()
    mag = np.array([0.55, 0.12, 0.65])
    blu = np.array([0.15, 0.25, 0.75])
    mix = np.clip(x * 0.8 + 0.1 * tint, 0, 1)[..., None]
    base[..., :3] += (neb[..., None] ** 1.6) * (mag * mix + blu * (1 - mix)) * 0.45
    base[..., 3] = 1
    # stars
    stars = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(stars)
    for _ in range(420):
        sx, sy = rng.random() * S, rng.random() * S
        rad = rng.choice([1.2, 1.6, 2.2, 3.0, 4.2]) * (S / 1024)
        a = rng.randint(90, 255)
        d.ellipse([sx - rad, sy - rad, sx + rad, sy + rad], fill=(255, 255, 255, a))
    for _ in range(14):  # sparkles
        sx, sy = rng.random() * S, rng.random() * S
        L = rng.uniform(14, 30) * S / 1024
        for dx, dy in ((L, 0), (0, L)):
            d.line([sx - dx, sy - dy, sx + dx, sy + dy], fill=(220, 230, 255, 110), width=int(1.5 * S / 1024))
    base = add_glow(base, stars, 3, 0.8)
    s = arr(stars)
    base[..., :3] += s[..., :3] * s[..., 3:4] * 0.9
    return base


# ------------------------------------------------------------------ orbits

def orbit_points(rx, ry, tilt, rot, n=1400):
    """Ellipse in 3D (rx, ry*cos(tilt), ry*sin(tilt)) rotated in-plane by `rot`. Returns (x, y, z, t)."""
    pts = []
    cx, cy = S * 0.5, S * 0.5
    for i in range(n + 1):
        t = i / n
        a = t * 2 * math.pi
        x0 = rx * math.cos(a)
        y0 = ry * math.sin(a) * math.cos(tilt)
        z0 = ry * math.sin(a) * math.sin(tilt)
        x = x0 * math.cos(rot) - y0 * math.sin(rot)
        y = x0 * math.sin(rot) + y0 * math.cos(rot)
        pts.append((cx + x, cy + y, z0, t))
    return pts


def ribbon_layers(pts, width, hue0, front: bool) -> Image.Image:
    """A twisting ribbon drawn as gap-free quads; colour shifts across and along the band (iridescence)."""
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    zmax = max(abs(p[2]) for p in pts) or 1
    bands = 12
    n = len(pts)

    def section(i):
        x, y, z, t = pts[i]
        xa, ya = pts[max(0, i - 1)][:2]
        xb, yb = pts[min(n - 1, i + 1)][:2]
        nx, ny = -(yb - ya), (xb - xa)
        ln = math.hypot(nx, ny) or 1
        depth = 0.45 + 0.55 * (z / zmax + 1) / 2
        twist = 0.3 + 0.7 * abs(math.cos(t * 2 * math.pi * 1.5 + hue0 * 4))
        w = width * depth * twist
        return x, y, nx / ln, ny / ln, w, depth, t, z

    secs = [section(i) for i in range(n)]
    for i in range(n - 1):
        x1, y1, nx1, ny1, w1, dp1, t1, z1 = secs[i]
        x2, y2, nx2, ny2, w2, dp2, t2, z2 = secs[i + 1]
        if (z1 >= 0) != front:
            continue
        for k in range(bands):
            u0 = k / bands * 2 - 1
            u1 = (k + 1) / bands * 2 - 1
            um = (u0 + u1) / 2
            quad = [(x1 + nx1 * u0 * w1 / 2, y1 + ny1 * u0 * w1 / 2), (x1 + nx1 * u1 * w1 / 2, y1 + ny1 * u1 * w1 / 2),
                    (x2 + nx2 * u1 * w2 / 2, y2 + ny2 * u1 * w2 / 2), (x2 + nx2 * u0 * w2 / 2, y2 + ny2 * u0 * w2 / 2)]
            c = pal(hue0 + t1 * 1.6 + um * 0.25 + 0.05 * math.sin(t1 * 23 + um * 3))
            edge = 1 - abs(um)
            bright = (0.55 + 0.55 * edge ** 0.6) * (0.45 + 0.65 * dp1)
            sheen = math.exp(-((um + 0.25) / 0.18) ** 2) * 0.55   # specular streak just off-centre
            col = tuple(int(min(255, v * bright + (255 - v * bright) * sheen)) for v in c) + (255,)
            d.polygon(quad, fill=col)
    return layer


# ------------------------------------------------------------------ data stack

def data_stack() -> Image.Image:
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    cx, cy = S * 0.5, S * 0.5
    W = S * 0.15
    H = S * 0.048
    T = S * 0.072
    gap = S * 0.03
    tops = [cy - 1.5 * T - gap - T * 0.1, cy - 0.5 * T - T * 0.1, cy + 0.5 * T + gap - T * 0.1]
    cols = [((255, 196, 96), (255, 64, 120)), ((255, 92, 150), (176, 58, 255)), ((150, 80, 255), (60, 120, 255))]
    for i in reversed(range(3)):
        y = tops[i]
        body = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        bd = ImageDraw.Draw(body)
        c1, c2 = cols[i]
        steps = 220
        for k in range(steps):
            u = k / (steps - 1)
            xx = cx - W + 2 * W * u
            light = 0.86 + 0.42 * math.exp(-((u - 0.3) / 0.2) ** 2) - 0.14 * u
            c = tuple(int(min(255, (c1[j] * (1 - u) + c2[j] * u) * light)) for j in range(3))
            hh = H * math.sqrt(max(0.0, 1 - (2 * u - 1) ** 2))
            bd.line([xx, y + hh, xx, y + T + hh], fill=c + (255,), width=int(2 * W / steps) + 2)
        # LED activity dots along the side: reads as storage
        for j in range(6):
            u = 0.18 + j * 0.11
            xx = cx - W + 2 * W * u
            hh = H * math.sqrt(max(0.0, 1 - (2 * u - 1) ** 2))
            yy = y + T * 0.55 + hh
            r = S * 0.0075
            on = (j + i) % 3 != 0
            bd.ellipse([xx - r, yy - r, xx + r, yy + r], fill=(200, 255, 255, 255) if on else (255, 255, 255, 80))
        top = tuple(int(min(255, (c1[j] * 0.5 + c2[j] * 0.5) * 0.75 + 255 * 0.3)) for j in range(3))
        bd.ellipse([cx - W, y - H, cx + W, y + H], fill=top + (255,))
        inner = tuple(int(min(255, v * 0.8 + 20)) for v in top)
        bd.ellipse([cx - W * 0.82, y - H * 0.78, cx + W * 0.82, y + H * 0.78], fill=inner + (255,))
        bd.ellipse([cx - W, y - H, cx + W, y + H], outline=(255, 250, 255, 235), width=int(S * 0.0045))
        refl = Image.new("L", (S, S), 0)
        ImageDraw.Draw(refl).ellipse([cx - W * 0.7, y - H * 0.7, cx - W * 0.05, y - H * 0.2], fill=150)
        refl = refl.filter(ImageFilter.GaussianBlur(S * 0.006))
        white = Image.new("RGBA", (S, S), (255, 255, 255, 0))
        white.putalpha(refl)
        body = Image.alpha_composite(body, white)
        layer = Image.alpha_composite(layer, body)
    return layer


def nodes(pts_list) -> Image.Image:
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for pts, picks in pts_list:
        for t in picks:
            x, y, z, _ = pts[int(t * (len(pts) - 1))]
            if z < 0:
                continue
            r = S * 0.012
            d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 255))
            r2 = S * 0.022
            d.ellipse([x - r2, y - r2, x + r2, y + r2], outline=(180, 240, 255, 220), width=int(S * 0.004))
    return layer


# ------------------------------------------------------------------ compose

def render() -> Image.Image:
    canvas = background()
    orbits = [
        orbit_points(S * 0.40, S * 0.31, math.radians(66), math.radians(-24)),
        orbit_points(S * 0.37, S * 0.33, math.radians(72), math.radians(38)),
    ]
    hues = [0.0, 0.45]
    widths = [S * 0.105, S * 0.09]
    back = [ribbon_layers(o, w, h, False) for o, w, h in zip(orbits, widths, hues)]
    front = [ribbon_layers(o, w, h, True) for o, w, h in zip(orbits, widths, hues)]
    for b in back:
        canvas = add_glow(canvas, b, S * 0.02, 0.55)
        canvas = over(canvas, arr(b) * np.array([0.8, 0.8, 0.8, 1.0], np.float32))
    stack = data_stack()
    canvas = add_glow(canvas, stack, S * 0.05, 0.9)
    canvas = over(canvas, arr(stack))
    for f in front:
        canvas = add_glow(canvas, f, S * 0.022, 0.6)
        canvas = over(canvas, arr(f))
    nd = nodes([(orbits[0], [0.08, 0.36]), (orbits[1], [0.18, 0.44])])
    canvas = add_glow(canvas, nd, S * 0.012, 1.4)
    canvas = over(canvas, arr(nd))
    canvas[..., :3] = np.clip(canvas[..., :3], 0, 1)
    # mask to rounded square, then rim + outer glow
    inset, radius = 0.075, 0.2
    mask = rounded_mask(S, inset, radius)
    rgb = img(canvas)
    rgb.putalpha(mask)
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rim)
    p = int(S * inset)
    for k in range(48):
        c = pal(0.05 + k / 48 * 0.9)
    # gradient rim: draw on a gradient image through a ring mask
    grad = Image.new("RGBA", (S, S))
    ga = np.zeros((S, S, 4), np.float32)
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32) / S
    tt = np.clip(xx * 0.5 + yy * 0.5, 0, 1)
    cols = np.array([pal(0.02 + v * 0.75) for v in np.linspace(0, 1, 256)], np.float32) / 255
    ga[..., :3] = cols[(tt * 255).astype(int)]
    ga[..., 3] = 1
    grad = img(ga)
    ring = Image.new("L", (S, S), 0)
    rdr = ImageDraw.Draw(ring)
    rdr.rounded_rectangle([p, p, S - p, S - p], radius=int(S * radius), outline=255, width=int(S * 0.004))
    rim = grad.copy()
    rim.putalpha(ring)
    glow_ring = grad.copy()
    glow_ring.putalpha(ring.filter(ImageFilter.GaussianBlur(S * 0.009)).point(lambda v: int(v * 0.8)))
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out = Image.alpha_composite(out, glow_ring)
    out = Image.alpha_composite(out, rgb)
    out = Image.alpha_composite(out, rim)
    return out.resize((1024, 1024), Image.LANCZOS)


def header(icon: Image.Image) -> Image.Image:
    Wd, Ht = 2560, 760
    yy, xx = np.mgrid[0:Ht, 0:Wd].astype(np.float32)
    g = np.zeros((Ht, Wd, 3), np.float32)
    left = np.array([0.30, 0.13, 0.62])
    mid = np.array([0.10, 0.10, 0.36])
    right = np.array([0.50, 0.12, 0.55])
    u = (xx / Wd)[..., None]
    g = np.where(u < 0.5, left * (1 - u * 2) + mid * (u * 2), mid * (2 - u * 2) + right * (u * 2 - 1))
    bg = Image.fromarray((g * 255).astype(np.uint8), "RGB").convert("RGBA")
    d = ImageDraw.Draw(bg)
    for _ in range(260):
        x, y, r = rng.random() * Wd, rng.random() * Ht, rng.choice([1, 1.5, 2, 3])
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, rng.randint(80, 220)))
    ic = icon.resize((560, 560), Image.LANCZOS)
    glow = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    glow.paste(ic, (180, 100), ic)
    bg = Image.alpha_composite(bg, glow.filter(ImageFilter.GaussianBlur(40)))
    bg.paste(ic, (180, 100), ic)
    font_b = ImageFont.truetype("/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf", 190) \
        if os.path.exists("/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf") else ImageFont.load_default()
    font_r = ImageFont.truetype("/usr/share/fonts/opentype/inter/Inter-Regular.otf", 40) \
        if os.path.exists("/usr/share/fonts/opentype/inter/Inter-Regular.otf") else ImageFont.load_default()
    font_c = ImageFont.truetype("/usr/share/fonts/opentype/inter/Inter-Medium.otf", 36) \
        if os.path.exists("/usr/share/fonts/opentype/inter/Inter-Medium.otf") else ImageFont.load_default()
    title = "Data Curator"
    tx, ty = 880, 170
    mask = Image.new("L", bg.size, 0)
    ImageDraw.Draw(mask).text((tx, ty), title, font=font_b, fill=255)
    tw = font_b.getbbox(title)[2]
    grad = np.zeros((Ht, Wd, 4), np.float32)
    uu = np.clip((xx - tx) / tw, 0, 1)
    cols = np.array([pal(v * 0.72) for v in np.linspace(0, 1, 256)], np.float32) / 255
    grad[..., :3] = cols[(uu * 255).astype(int)]
    grad[..., 3] = 1
    gimg = img(grad)
    gimg.putalpha(mask)
    tglow = gimg.filter(ImageFilter.GaussianBlur(18))
    bg = Image.alpha_composite(bg, tglow)
    bg = Image.alpha_composite(bg, gimg)
    d = ImageDraw.Draw(bg)
    d.text((tx + 6, 430), "Organizes music, TV, movies, documents and photos — preview first, undo anytime.",
           font=font_r, fill=(236, 232, 255))
    chips = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    cd = ImageDraw.Draw(chips)
    x = tx + 6
    for chip in ("macOS", "Windows", "Linux", "Music & TV", "Journaled", "Undo"):
        w = font_c.getbbox(chip)[2] + 52
        cd.rounded_rectangle([x, 520, x + w, 586], radius=33, fill=(255, 255, 255, 30),
                             outline=(255, 255, 255, 110), width=2)
        cd.text((x + 26, 531), chip, font=font_c, fill=(244, 240, 255, 255))
        x += w + 18
    bg = Image.alpha_composite(bg, chips)
    return bg.convert("RGB")


def main():
    os.makedirs(OUT, exist_ok=True)
    icon = render()
    icon.save(os.path.join(OUT, "AppIcon.png"))
    for s in (16, 32, 48, 64, 128, 256, 512):
        icon.resize((s, s), Image.LANCZOS).save(os.path.join(OUT, f"icon-{s}.png"))
    icon.save(os.path.join(OUT, "AppIcon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                                       (128, 128), (256, 256)])
    icon.save(os.path.join(OUT, "AppIcon.icns"))
    header(icon).save(os.path.join(ROOT, "docs", "readme-header.png"))
    print("icon written to", OUT)


if __name__ == "__main__":
    main()
