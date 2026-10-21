"""Typed ordinary private memory; absent sightings remain explicitly unknown."""

from typing import Literal

from pydantic import Field, StrictInt

from .plan_protocol import PrivateModel

Position = tuple[StrictInt, StrictInt]
Alias = Literal["Cog-A", "Cog-B", "Cog-C", "Cog-D"]


class AllyView(PrivateModel):
    alias: Alias
    pos: Position | None
    age: StrictInt = Field(ge=0)
    seen: StrictInt | None
    carrying: str


class BoardView(PrivateModel):
    age: StrictInt | None
    salad: StrictInt = Field(ge=0)
    soup: StrictInt = Field(ge=0)
    fries: StrictInt = Field(ge=0)


class StationView(PrivateModel):
    name: str
    pos: Position
    age: StrictInt = Field(ge=0)
    note: str


class KitchenView(PrivateModel):
    turn: StrictInt = Field(ge=0)
    turns: StrictInt = Field(gt=0)
    tick: StrictInt = Field(ge=0)
    ticks: StrictInt = Field(gt=0)
    layout: str
    dishes: StrictInt = Field(ge=0)
    live: StrictInt = Field(ge=0)
    alias: Alias
    position: Position
    carrying: str
    delivered: StrictInt = Field(ge=0)
    team: list[AllyView] = Field(max_length=3)
    board: BoardView
    stations: list[StationView] = Field(max_length=9)
    counters: list[tuple[Position, str]]
    blocked: StrictInt = Field(ge=0)
    blocked_at: Position | None
    legal_stations: list[str] = Field(min_length=1, max_length=10)
    last_order: str
    radio: list[tuple[str, str]] = Field(max_length=3)
    note: str = Field(max_length=200)
