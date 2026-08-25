"""Key, split and pad the nano-banana cog sheet into the four facing sprites.

    python3 scripts/art/split_cog_sheet.py

Reads `scripts/art/source/cog_chef_sheet.png` (four cogs in a row on a flat
chroma backdrop) and writes `data/art/cog_{south,east,north,west}.png` -- the
sprites the wasm replay viewer preloads and tints per seat.

Gemini does not return alpha, and the "pure green" you asked for comes back as
*some* green with a tinted edge, so the backdrop colour is taken as the median
of the image border and keyed with a tolerance; the fill runs from the border
inward so a green pixel inside the character survives.

The sheet's third and fourth poses both came back as the cog's back, so `west`
is the mirror of the `east` profile -- one render, four honest facings.
"""

from __future__ import annotations

import statistics
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SHEET = ROOT / "scripts" / "art" / "source" / "cog_chef_sheet.png"
OUT_DIR = ROOT / "data" / "art"
SIZE = 96
TOLERANCE = 60
# Sheet order: front, right profile, back, back. West is the mirrored profile.
FACINGS = ["south", "east", "north"]


def border_colour(image: Image.Image) -> tuple[int, int, int]:
    pixels = image.load()
    width, height = image.size
    samples: list[tuple[int, int, int]] = []
    for x in range(0, width, 4):
        samples.append(pixels[x, 0][:3])
        samples.append(pixels[x, height - 1][:3])
    for y in range(0, height, 4):
        samples.append(pixels[0, y][:3])
        samples.append(pixels[width - 1, y][:3])
    return (
        int(statistics.median(s[0] for s in samples)),
        int(statistics.median(s[1] for s in samples)),
        int(statistics.median(s[2] for s in samples)),
    )


def key_out(image: Image.Image, key: tuple[int, int, int]) -> Image.Image:
    """Flood the backdrop from the border so interior greens survive."""
    image = image.convert("RGBA")
    width, height = image.size
    pixels = image.load()

    def is_key(x: int, y: int) -> bool:
        r, g, b, _a = pixels[x, y]
        return (
            abs(r - key[0]) <= TOLERANCE
            and abs(g - key[1]) <= TOLERANCE
            and abs(b - key[2]) <= TOLERANCE
        )

    stack = []
    for x in range(width):
        stack.append((x, 0))
        stack.append((x, height - 1))
    for y in range(height):
        stack.append((0, y))
        stack.append((width - 1, y))
    seen = set()
    while stack:
        x, y = stack.pop()
        if (x, y) in seen or not (0 <= x < width and 0 <= y < height):
            continue
        seen.add((x, y))
        if not is_key(x, y):
            continue
        pixels[x, y] = (0, 0, 0, 0)
        stack.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    return image


def columns_with_ink(image: Image.Image) -> list[int]:
    width, height = image.size
    pixels = image.load()
    return [
        x for x in range(width) if any(pixels[x, y][3] > 24 for y in range(0, height, 2))
    ]


def split(image: Image.Image) -> list[Image.Image]:
    ink = columns_with_ink(image)
    if not ink:
        raise SystemExit("nothing survived the chroma key; check the tolerance")
    runs: list[tuple[int, int]] = []
    start = previous = ink[0]
    for x in ink[1:]:
        if x - previous > 8:
            runs.append((start, previous))
            start = x
        previous = x
    runs.append((start, previous))
    runs = [run for run in runs if run[1] - run[0] > 12]
    parts = []
    for left, right in runs:
        box = image.crop((left, 0, right + 1, image.height))
        parts.append(box.crop(box.getbbox()))
    return parts


def pad_square(image: Image.Image, size: int) -> Image.Image:
    width, height = image.size
    side = max(width, height)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(image, ((side - width) // 2, side - height))
    return canvas.resize((size, size), Image.LANCZOS)


def main() -> None:
    sheet = Image.open(SHEET)
    keyed = key_out(sheet, border_colour(sheet.convert("RGB")))
    parts = split(keyed)
    if len(parts) < len(FACINGS):
        raise SystemExit(f"expected >= {len(FACINGS)} cogs on the sheet, found {len(parts)}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for facing, part in zip(FACINGS, parts, strict=False):
        out = OUT_DIR / f"cog_{facing}.png"
        pad_square(part, SIZE).save(out)
        print(f"wrote {out}")
    west = pad_square(parts[1], SIZE).transpose(Image.FLIP_LEFT_RIGHT)
    west.save(OUT_DIR / "cog_west.png")
    print(f"wrote {OUT_DIR / 'cog_west.png'}")


if __name__ == "__main__":
    main()
