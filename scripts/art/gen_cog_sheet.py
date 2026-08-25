"""Generate the cog chef sheet with nano-banana (Gemini image generation).

Four facings of the Softmax cog in a kitchen apron, on a flat chroma backdrop,
anchored to the canonical cog render so the character is OURS and not a
generic robot. Run once; the source sheet and the split script are committed
because CI does not regenerate art.

    GEMINI_API_KEY=... python3 scripts/art/gen_cog_sheet.py

The key is only ever the `x-goog-api-key` header: never printed, never written
to a file, never a URL parameter.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "scripts" / "art" / "source" / "cog_reference.png"
OUT = ROOT / "scripts" / "art" / "source" / "cog_chef_sheet.png"
ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-image:generateContent"
)

PROMPT = """Using this wheeled robot character ("cog") as the EXACT character design
reference -- same boxy screen head with two glowing cyan eyes and a small smile, same
riveted shoulders, same three-wheel chassis -- draw FOUR of these cogs in ONE horizontal
row, evenly spaced, identical size, full body, same clean cartoon rendering, no outlines
added. The cog is now a line cook: it wears a white chef's apron over its chest plate and
a small white folded chef's toque on top of the screen head. Body plating is neutral
light steel grey so it can be team-tinted later.
The FOUR poses, left to right, are the SAME cog seen from four directions:
1) facing the viewer (front, screen and both eyes visible),
2) facing to the RIGHT (side profile, screen edge-on to the right),
3) facing AWAY from the viewer (back of the head, no eyes, apron strings tied in a bow),
4) facing to the LEFT (side profile, screen edge-on to the left).
Both arms are held forward at waist height in every pose, empty, palms up, as if about to
receive a plate.
Background: perfectly flat, solid, uniform pure bright green (#00FF00), no shadows, no
gradient, no floor, no props, no text, no labels -- it will be chroma-keyed out."""


def main() -> int:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("GEMINI_API_KEY is not set", file=sys.stderr)
        return 2
    reference = base64.b64encode(REFERENCE.read_bytes()).decode()
    body = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/png", "data": reference}},
                    {"text": PROMPT},
                ]
            }
        ],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        headers={"x-goog-api-key": key, "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = json.load(response)
    part = next(p for p in payload["candidates"][0]["content"]["parts"] if "inlineData" in p)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(base64.b64decode(part["inlineData"]["data"]))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
