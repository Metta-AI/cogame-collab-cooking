"""The eight kitchens: shape, station census, connectivity, corridor width."""

from __future__ import annotations

import pytest
from mettagrid.map_builder.ascii import AsciiMapBuilderConfig

from collab_cooking.kitchens import layouts as L
from collab_cooking.missions.kitchen import make_kitchen_mission

STATION_NAMES = sorted(L.STATION_CHARS.values())
CONNECTED_KITCHENS = [name for name in L.LAYOUT_NAMES if name != "forced"]


@pytest.mark.parametrize("layout", L.LAYOUT_NAMES)
def test_rows_are_rectangular(layout: str) -> None:
    rows = L.grid(layout)
    assert len({len(row) for row in rows}) == 1, "every row must be the same width"
    width, height = L.dimensions(layout)
    assert (width, height) == (len(rows[0]), len(rows))


@pytest.mark.parametrize("layout", L.LAYOUT_NAMES)
def test_border_is_solid(layout: str) -> None:
    width, height = L.dimensions(layout)
    tiles = L.open_tiles(layout)
    for x in range(width):
        assert (0, x) not in tiles
        assert (height - 1, x) not in tiles
    for y in range(height):
        assert (y, 0) not in tiles
        assert (y, width - 1) not in tiles


@pytest.mark.parametrize("layout", L.LAYOUT_NAMES)
def test_exactly_one_of_each_station_and_four_spawns(layout: str) -> None:
    stations = L.stations(layout)
    assert sorted(stations) == STATION_NAMES
    counts: dict[str, int] = {}
    for row in L.grid(layout):
        for char in row:
            if char in L.STATION_CHARS:
                counts[char] = counts.get(char, 0) + 1
    assert set(counts.values()) == {1}, f"a station appears twice in {layout}: {counts}"
    assert len(L.spawns(layout)) == 4


@pytest.mark.parametrize("layout", L.LAYOUT_NAMES)
def test_every_station_touches_an_open_tile(layout: str) -> None:
    tiles = L.open_tiles(layout)
    for kind, (row, col) in L.stations(layout).items():
        neighbours = {(row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)}
        assert neighbours & tiles, f"{kind} in {layout} is walled in"


@pytest.mark.parametrize("layout", CONNECTED_KITCHENS)
def test_connected_kitchens_have_one_component(layout: str) -> None:
    assert len(L.components(layout)) == 1


def test_forced_has_exactly_two_sealed_halves() -> None:
    components = L.components("forced")
    assert len(components) == 2
    divider = L.divider_counters("forced")
    assert len(divider) >= 4, "an item must have somewhere to cross"
    tiles = L.open_tiles("forced")
    for row, col in divider:
        vertical = (row - 1, col) in tiles and (row + 1, col) in tiles
        horizontal = (row, col - 1) in tiles and (row, col + 1) in tiles
        assert vertical or horizontal
    # And the board is adjacent to BOTH halves, so neither side is blind.
    board = L.stations("forced")["order_board"]
    touched = {
        index
        for index, component in enumerate(components)
        for neighbour in ((board[0], board[1] - 1), (board[0], board[1] + 1))
        if neighbour in component
    }
    assert len(touched) == 2


def test_crowded_has_exactly_one_passage_cell() -> None:
    # The divider is column 5; exactly one cell of it is open.
    tiles = L.open_tiles("crowded")
    _width, height = L.dimensions("crowded")
    passage = [(row, 5) for row in range(height) if (row, 5) in tiles]
    assert passage == [(3, 5)]
    assert len(L.components("crowded")) == 1


@pytest.mark.parametrize("layout", ["ring", "figure-eight"])
def test_one_tile_corridors_have_no_two_by_two_open_block(layout: str) -> None:
    tiles = L.open_tiles(layout)
    width, height = L.dimensions(layout)
    for row in range(height - 1):
        for col in range(width - 1):
            block = {(row, col), (row, col + 1), (row + 1, col), (row + 1, col + 1)}
            assert not block <= tiles, f"{layout} has a 2x2 open block at {(row, col)}"


@pytest.mark.parametrize("layout", L.LAYOUT_NAMES)
def test_kitchen_loads_through_the_ascii_builder(layout: str) -> None:
    builder = L.kitchen(layout)
    assert isinstance(builder, AsciiMapBuilderConfig)
    env = make_kitchen_mission(layout, 60)
    assert env.game.num_agents == 4


@pytest.mark.parametrize("layout", L.LAYOUT_NAMES)
def test_spawns_are_assigned_in_reading_order(layout: str) -> None:
    # The plan-directive layer converts between the brain's spawn-relative
    # frame and absolute kitchen coordinates through exactly this assumption.
    from mettagrid.simulator import Simulator

    sim = Simulator().new_simulation(make_kitchen_mission(layout, 20), seed=1)
    placed = {
        int(obj["agent_id"]): (int(obj["r"]), int(obj["c"]))
        for obj in sim.grid_objects().values()
        if obj.get("type_name") == "agent"
    }
    assert [placed[slot] for slot in range(4)] == L.spawns(layout)


def test_reachable_stations_respects_the_forced_divider() -> None:
    left = L.reachable_stations("forced", (2, 3))
    right = L.reachable_stations("forced", (2, 9))
    assert "pot" not in left and "fryer" not in left and "pass" not in left
    assert "pot" in right and "fryer" in right and "pass" in right
    # The board is reachable from both, and `hold` is always legal.
    assert "board" in left and "board" in right
    assert left[-1] == "hold" and right[-1] == "hold"


def test_unknown_layout_is_a_clear_error() -> None:
    with pytest.raises(ValueError, match="unknown layout"):
        L.grid("no-such-kitchen")
