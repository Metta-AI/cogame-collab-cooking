#!/usr/bin/env python3
"""Build client/replay_broadcast.html from coworld-ctf's page.

The rule the playbook pins is "the starter's page PLUS an appended game
block", never a from-scratch page that reuses the starter's ids. So this
script takes ctf's `client/replay_broadcast.html`, keeps its `<head>`, its CSS
custom properties, its `#chrome` / `#stage` / `#board` / `#viewport` /
`#bannerlane`, the transport markup and the endcard skeleton exactly as they
are, deletes only the elements the design note lists, and appends the
collab-cooking CSS and game block under a banner comment.

    python3 tools/build_broadcast_page.py /path/to/coworld-ctf

It is committed so the provenance of every line in the page is checkable: run
it against a fresh ctf checkout and diff.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANNER = "collab-cooking additions to the inherited coworld-ctf chrome"

# Elements the design note removes: ctf-specific, replaced by the game block.
# #killfeed becomes #feed. #momentum, #lulls and #win-chip are KEPT as empty
# nodes because chrome_common.js resolves them by getElementById.
DROP_BLOCKS = [
    ("<!-- Pre-load curtain: the bot locker room.", "</div>\n\n  <div id=\"chrome\">", "\n  <div id=\"chrome\">"),
]


def cut_element(html: str, marker: str) -> str:
    """Remove the balanced <div> that starts at `marker` (an id= attribute)."""
    start = html.index(marker)
    start = html.rindex("<", 0, start)
    depth = 0
    index = start
    while index < len(html):
        if html.startswith("<div", index) or html.startswith("<svg", index):
            depth += 1
            index = html.index(">", index) + 1
            continue
        if html.startswith("</div>", index) or html.startswith("</svg>", index):
            depth -= 1
            index += 6
            if depth == 0:
                return html[:start] + html[index:]
            continue
        index += 1
    raise SystemExit(f"unbalanced element at {marker}")


def main() -> None:
    ctf = Path(sys.argv[1] if len(sys.argv) > 1 else "/workspace/starters/coworld-ctf")
    src = (ctf / "client" / "replay_broadcast.html").read_text(encoding="utf-8")
    head, rest = src.split("</style>\n</head>\n<body>\n", 1)
    body, _script = rest.split("\n<!-- WIRE_CONSTANTS -->", 1)

    for marker in (
        'id="lockerroom"',
        'id="viewpanel"',
        'id="fpv"',
        'id="povBadge"',
        'id="mmwarn"',
        'id="killfeed"',
    ):
        body = cut_element(body, marker)
    # The comment blocks that introduced the removed elements go with them.
    body = re.sub(r"\n *<!-- Pre-load curtain:.*?-->", "", body, flags=re.S)
    body = re.sub(r"\n *<!-- View controls:.*?-->", "", body, flags=re.S)
    body = re.sub(r"\n *<!-- First-person picture-in-picture:.*?-->", "", body, flags=re.S)
    body = re.sub(r"\n{3,}", "\n\n", body)

    game_body = (ROOT / "client" / "parts" / "game_body.html").read_text(encoding="utf-8")
    game_css = (ROOT / "client" / "parts" / "game.css").read_text(encoding="utf-8")
    game_js = (ROOT / "client" / "parts" / "game.js").read_text(encoding="utf-8")

    body = body.replace('  <div id="status">connecting</div>', game_body.rstrip("\n"))

    out = (
        head
        + f"\n/* ===== {BANNER} ===== */\n"
        + game_css.rstrip("\n")
        + "\n</style>\n</head>\n<body>\n"
        + body
        + "\n<!-- CHROME_COMMON -->\n<!-- BROADCAST_CORE -->\n\n<script>\n"
        + f"// ===== {BANNER} =====\n"
        + game_js.rstrip("\n")
        + "\n</script>\n</body>\n</html>\n"
    )
    (ROOT / "client" / "replay_broadcast.html").write_text(out, encoding="utf-8")
    print(f"wrote client/replay_broadcast.html ({len(out)} bytes)")


if __name__ == "__main__":
    main()
