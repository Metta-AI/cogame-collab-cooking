"""The eight hand-authored kitchens.

Every kitchen is a fixed ASCII grid loaded through
``mettagrid.map_builder.ascii.AsciiMapBuilderConfig`` -- the same builder the
starter's ``classic/map.py`` uses. It is deliberately NOT a procedural hub:
``CompoundConfig.layout`` is a ``Literal`` in the external ``mettagrid``
package and we are not adding enum members to a dependency to draw a kitchen.

Each kitchen contains exactly one of each of the nine stations and exactly
four spawns, so the only thing that differs between variants is geometry.
"""

from __future__ import annotations

from collections import deque

from mettagrid.map_builder.ascii import AsciiMapBuilderConfig

CHAR_TO_MAP_NAME: dict[str, str] = {
    "#": "wall",
    ".": "empty",
    "V": "veg_station",
    "M": "meat_station",
    "L": "plate_station",
    "X": "chopping_station",
    "O": "cooking_station",
    "F": "fryer_station",
    "S": "serving_station",
    "W": "wash_station",
    "B": "order_board",
    "@": "agent.agent",
}

STATION_CHARS: dict[str, str] = {
    "V": "veg_station",
    "M": "meat_station",
    "L": "plate_station",
    "X": "chopping_station",
    "O": "cooking_station",
    "F": "fryer_station",
    "S": "serving_station",
    "W": "wash_station",
    "B": "order_board",
}

# The short names a plan's `station` field may name, and the station object
# each one routes to.
STATION_BY_PLAN_NAME: dict[str, str] = {
    "veg": "veg_station",
    "meat": "meat_station",
    "plate": "plate_station",
    "chop": "chopping_station",
    "pot": "cooking_station",
    "fryer": "fryer_station",
    "pass": "serving_station",
    "sink": "wash_station",
    "board": "order_board",
}
PLAN_NAME_BY_STATION: dict[str, str] = {v: k for k, v in STATION_BY_PLAN_NAME.items()}

OPEN_CHARS = frozenset({".", "@"})


KITCHENS: dict[str, list[str]] = {
    # The control case: no forced hand-off, no choke. Everything is reachable
    # both ways round the central counter island.
    "open-kitchen": [
        "#####V#M#####",
        "#...........#",
        "#..@.....@..#",
        "#B..#####..X#",
        "#...#####...#",
        "#L..#####..O#",
        "#..@.....@..#",
        "#...........#",
        "#####W#S#F###",
    ],
    # Melting Pot `cramped_room`: a 7x5 interior for four cogs. Isolates
    # personal space, and is the certification / smoke fixture (smallest).
    "cramped": [
        "###V#M###",
        "#...@...#",
        "#B.....X#",
        "#.@...@.#",
        "#L.....O#",
        "#...@...#",
        "###W#S#F#",
    ],
    # Melting Pot `forced`: two sealed halves. The six divider counters are the
    # only way an item crosses; the order board sits IN the divider so neither
    # side is blind to the tickets.
    "forced": [
        "##V#M########",
        "#.....#.....#",
        "#..@..#..@..#",
        "#X....#....O#",
        "#.....B.....#",
        "#L....#....F#",
        "#..@..#..@..#",
        "#.....#.....#",
        "####W####S###",
    ],
    # Melting Pot `crowded` at four seats: the same split as `forced` except
    # the divider has exactly one gap, so every ingredient and every plate goes
    # through one tile.
    "crowded": [
        "##V#M##B###",
        "#.@..#..@.#",
        "#X...#...O#",
        "#.........#",
        "#L...#...F#",
        "#.@..#..@.#",
        "##W####S###",
    ],
    # Melting Pot `asymmetric_advantages`: the right half owns the pot, the
    # fryer and the pass; the left half owns everything else. Connected, but
    # unequal -- which is the point.
    "asymmetric": [
        "###V#M#####B###",
        "#.............#",
        "#..@..###..@..#",
        "#X....###....O#",
        "#.....###....F#",
        "#L....###....S#",
        "#..@..###..@..#",
        "#.............#",
        "#####W#########",
    ],
    # Melting Pot `counter_circuit`: walking round the island is 12 steps,
    # putting the item on it and letting the other side take it is 2.
    "circuit": [
        "###V#M###B#####",
        "#.............#",
        "#..@.......@..#",
        "#X..#######..O#",
        "#..@.......@..#",
        "#.............#",
        "####L###W#S#F##",
    ],
    # Melting Pot `ring`: a one-tile-wide corridor all the way round a solid
    # block. Two cogs meeting head-on cannot pass.
    "ring": [
        "###V#M#B###",
        "#..@...@..#",
        "#.#######.#",
        "X.#######.O",
        "#.#######.#",
        "L.#######.F",
        "#.#######.#",
        "#..@...@..#",
        "###W#S#####",
    ],
    # Melting Pot `figure_eight`: two one-tile loops sharing the central
    # column. Everything crossing between loops fights for the same spine.
    "figure-eight": [
        "####V#M#B######",
        "#.@.........@.#",
        "#.#####.#####.#",
        "X.#####.#####.O",
        "#.#####.#####.#",
        "L.#####.#####.F",
        "#.#####.#####.#",
        "#.@.........@.#",
        "####W###S######",
    ],
}

LAYOUT_NAMES: tuple[str, ...] = tuple(KITCHENS)

# One fixed sentence per kitchen, handed to the LLM as `{layout_line}`.
LAYOUT_LINES: dict[str, str] = {
    "open-kitchen": "one counter island in the middle; everything is reachable both ways round",
    "cramped": "a 7x5 room for four cogs: two cogs cannot pass without one giving way",
    "forced": (
        "two sealed halves - nothing crosses except over the six counter cells in the middle wall"
    ),
    "crowded": "prep on the left, cooking and the pass on the right, joined by ONE open tile",
    "asymmetric": (
        "the right half owns the pot, the fryer and the pass; the left half owns everything else"
    ),
    "circuit": (
        "a counter island down the middle: walking round it is 12 steps, handing over it is 2"
    ),
    "ring": "a one-tile corridor all the way round: two cogs cannot pass",
    "figure-eight": "two one-tile loops sharing one central spine everything must cross",
}

LAYOUT_BLURBS: dict[str, str] = {
    "open-kitchen": "the control: no forced hand-off, no choke",
    "cramped": "four cogs in a 7x5 room",
    "forced": "two sealed halves; items only over the counter",
    "crowded": "one choke tile between prep and service",
    "asymmetric": "unequal station access -> task allocation",
    "circuit": "hand off over the island or walk 12 tiles",
    "ring": "a one-tile corridor: right of way",
    "figure-eight": "two loops, one shared spine",
}


def grid(layout: str) -> list[str]:
    """The raw ASCII rows for `layout`."""
    try:
        return list(KITCHENS[layout])
    except KeyError:
        raise ValueError(
            f"unknown layout {layout!r}; expected one of {', '.join(LAYOUT_NAMES)}"
        ) from None


def dimensions(layout: str) -> tuple[int, int]:
    """(width, height) in tiles."""
    rows = grid(layout)
    return len(rows[0]), len(rows)


def kitchen(layout: str) -> AsciiMapBuilderConfig:
    """The map builder config for `layout`."""
    rows = grid(layout)
    return AsciiMapBuilderConfig(
        map_data=[list(row) for row in rows],
        char_to_map_name=dict(CHAR_TO_MAP_NAME),
    )


def open_tiles(layout: str) -> set[tuple[int, int]]:
    """Every walkable tile as (row, col). Spawns are walkable."""
    rows = grid(layout)
    return {
        (r, c)
        for r, row in enumerate(rows)
        for c, ch in enumerate(row)
        if ch in OPEN_CHARS
    }


def spawns(layout: str) -> list[tuple[int, int]]:
    rows = grid(layout)
    return [
        (r, c) for r, row in enumerate(rows) for c, ch in enumerate(row) if ch == "@"
    ]


def stations(layout: str) -> dict[str, tuple[int, int]]:
    """station object name -> (row, col). Exactly nine entries."""
    rows = grid(layout)
    found: dict[str, tuple[int, int]] = {}
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            name = STATION_CHARS.get(ch)
            if name is not None:
                found[name] = (r, c)
    return found


def counters(layout: str) -> set[tuple[int, int]]:
    """Every `#` cell -- walls are counters here, you can put one item down."""
    rows = grid(layout)
    return {
        (r, c)
        for r, row in enumerate(rows)
        for c, ch in enumerate(row)
        if ch == "#"
    }


def _neighbours(pos: tuple[int, int]):
    r, c = pos
    yield (r - 1, c)
    yield (r + 1, c)
    yield (r, c - 1)
    yield (r, c + 1)


def components(layout: str) -> list[set[tuple[int, int]]]:
    """Connected components of the open tiles, cardinal adjacency."""
    remaining = open_tiles(layout)
    found: list[set[tuple[int, int]]] = []
    while remaining:
        start = min(remaining)
        seen = {start}
        queue = deque([start])
        while queue:
            here = queue.popleft()
            for nxt in _neighbours(here):
                if nxt in remaining and nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        found.append(seen)
        remaining -= seen
    return found


def reachable_tiles(layout: str, tile: tuple[int, int]) -> set[tuple[int, int]]:
    """The open tiles reachable from `tile` (which need not itself be open)."""
    tiles = open_tiles(layout)
    seeds = [tile] if tile in tiles else [n for n in _neighbours(tile) if n in tiles]
    seen: set[tuple[int, int]] = set()
    queue = deque()
    for seed in seeds:
        if seed not in seen:
            seen.add(seed)
            queue.append(seed)
    while queue:
        here = queue.popleft()
        for nxt in _neighbours(here):
            if nxt in tiles and nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def reachable_stations(layout: str, tile: tuple[int, int]) -> list[str]:
    """The plan-level station names reachable from `tile`, plus `hold`.

    This is the predicate that builds `LEGAL STATIONS` for the prompt AND the
    one the reply validator applies, so a left-half cog in `forced` is never
    offered `pot`, `fryer` or `pass` and can never be told it chose one
    illegally after being offered it.
    """
    reach = reachable_tiles(layout, tile)
    legal: list[str] = []
    for station_name, pos in stations(layout).items():
        if any(n in reach for n in _neighbours(pos)):
            legal.append(PLAN_NAME_BY_STATION[station_name])
    legal.sort(key=lambda name: list(STATION_BY_PLAN_NAME).index(name))
    legal.append("hold")
    return legal


def divider_counters(layout: str) -> set[tuple[int, int]]:
    """Counter cells with an open tile on BOTH sides.

    In `forced` and `crowded` this is exactly the divider -- the only way an
    item crosses.
    """
    tiles = open_tiles(layout)
    out: set[tuple[int, int]] = set()
    for r, c in counters(layout):
        if ((r - 1, c) in tiles and (r + 1, c) in tiles) or (
            (r, c - 1) in tiles and (r, c + 1) in tiles
        ):
            out.add((r, c))
    return out


def pass_counters(layout: str) -> set[tuple[int, int]]:
    """The `zone: "pass"` target set, and the hand-off staging set.

    In `forced` / `crowded` these are the divider cells (open on both sides);
    elsewhere they are the central island's usable flanks -- every interior
    (non-border) counter with an open tile beside it. `cramped` has no island
    at all, so its set is empty and `zone: "pass"` degrades to unrestricted
    (see `in_zone`).
    """
    width, height = dimensions(layout)
    tiles = open_tiles(layout)
    out = divider_counters(layout)
    for r, c in counters(layout):
        if r in (0, height - 1) or c in (0, width - 1):
            continue
        if any(n in tiles for n in _neighbours((r, c))):
            out.add((r, c))
    return out


def zone_of(layout: str, pos: tuple[int, int]) -> str:
    """`left` / `right` for a tile, by the design's `col < width // 2` rule."""
    width, _ = dimensions(layout)
    return "left" if pos[1] < width // 2 else "right"


def in_zone(layout: str, pos: tuple[int, int], zone: str) -> bool:
    if zone == "any":
        return True
    if zone == "pass":
        staging = pass_counters(layout)
        # A kitchen with no island and no divider (cramped) has nowhere to
        # stage: leaving the goal unrestricted keeps the cog playing.
        return not staging or pos in staging
    return zone_of(layout, pos) == zone
