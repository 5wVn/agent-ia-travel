#!/usr/bin/env python3
"""Génère les icônes PWA d'Agent Travel (best effort, sans dépendance runtime).

Fond brun espresso, monogramme « AT » crème, coins pleins (full bleed, pas de
rayon — l'OS applique son propre masque). Les glyphes A et T sont dessinés en
primitives vectorielles (polygones / rectangles) pour rester nets à toutes les
tailles sans embarquer de police TTF.

Sortie : app/static/icons/icon-192.png, icon-512.png, apple-touch-180.png.

Lancer (Pillow requis, installé dans le venv uniquement, hors requirements) :
    .venv/bin/python scripts/gen_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

# Couleurs alignées sur la palette dark (espresso) du dashboard.
BG = (30, 23, 17)        # --bg dark  #1E1711
CREAM = (237, 230, 218)  # --text dark #EDE6DA
TERRA = (184, 116, 58)   # --accent dark #B8743A

ICONS_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"

# On dessine en très haute résolution puis on réduit : antialiasing propre.
SUPER = 1024


def _draw_monogram(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)

    # Zone utile centrée (le glyphe occupe ~58 % de la largeur).
    glyph_w = size * 0.62
    glyph_h = size * 0.42
    cx, cy = size / 2.0, size / 2.0
    left = cx - glyph_w / 2.0
    top = cy - glyph_h / 2.0
    bar = size * 0.072          # épaisseur des fûts
    gap = size * 0.052          # espace entre A et T

    # Largeurs respectives : A un peu plus large que T.
    a_w = glyph_w * 0.50
    t_w = glyph_w - a_w - gap
    a_left = left
    t_left = left + a_w + gap

    # ----- Lettre A (deux jambages + barre transversale) -----
    apex_x = a_left + a_w / 2.0
    apex_y = top
    base_y = top + glyph_h
    # jambage gauche
    d.polygon(
        [
            (apex_x - bar / 2.0, apex_y),
            (apex_x + bar / 2.0, apex_y),
            (a_left + bar, base_y),
            (a_left, base_y),
        ],
        fill=CREAM,
    )
    # jambage droit
    d.polygon(
        [
            (apex_x - bar / 2.0, apex_y),
            (apex_x + bar / 2.0, apex_y),
            (a_left + a_w, base_y),
            (a_left + a_w - bar, base_y),
        ],
        fill=CREAM,
    )
    # barre transversale (accent terracotta — clin d'œil à l'accent du dashboard)
    crossbar_y = top + glyph_h * 0.62
    d.rectangle(
        [a_left + a_w * 0.18, crossbar_y - bar * 0.42,
         a_left + a_w * 0.82, crossbar_y + bar * 0.42],
        fill=TERRA,
    )

    # ----- Lettre T (chapeau + fût) -----
    # chapeau
    d.rectangle([t_left, top, t_left + t_w, top + bar], fill=CREAM)
    # fût centré
    t_cx = t_left + t_w / 2.0
    d.rectangle(
        [t_cx - bar / 2.0, top, t_cx + bar / 2.0, top + glyph_h],
        fill=CREAM,
    )

    return img


def make(size: int, path: Path) -> None:
    big = _draw_monogram(SUPER)
    out = big.resize((size, size), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(path, format="PNG", optimize=True)
    print(f"écrit {path.relative_to(path.parents[3])} ({size}x{size})")


def main() -> None:
    make(192, ICONS_DIR / "icon-192.png")
    make(512, ICONS_DIR / "icon-512.png")
    make(180, ICONS_DIR / "apple-touch-180.png")


if __name__ == "__main__":
    main()
