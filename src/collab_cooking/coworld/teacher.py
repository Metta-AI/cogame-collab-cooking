"""A source-owned macro teacher that reads only the ordinary private window."""

from .decision_view import KitchenView
from .llm import build_user_message
from .plan_protocol import PlanControl, PlanWindow
from .plans import parse_plan

TEACHER_POLICY = "private-kitchen-macro-v1"


def decide(window: PlanWindow) -> PlanControl:
    view = KitchenView.model_validate(window.observation)
    if (
        view.legal_stations != window.legal_stations
        or view.alias != window.alias
        or view.turn != window.turn
        or view.tick != window.step
        or build_user_message(view.model_dump()) != window.user
    ):
        raise ValueError("teacher window differs from its source private observation")
    recipe = "any"
    if view.board.age is not None:
        recipe = next((name for name in ("soup", "fries", "salad") if getattr(view.board, name) > 0), "any")
    carried = view.carrying
    if carried.startswith("dish_"):
        station = "pass"
    elif carried == "dirty_plate":
        station = "sink"
    elif carried in ("veg", "meat"):
        station = "chop"
    elif carried in ("chopped_veg", "chopped_meat"):
        station = "pot" if recipe == "soup" else "fryer" if recipe == "fries" else "pass"
    elif carried == "clean_plate":
        station = "pot" if recipe == "soup" else "fryer" if recipe == "fries" else "pass"
    elif recipe != "any":
        # Anonymous cogs distribute the ordinary prep roles without hidden inventory.
        station = ("veg", "meat", "plate", "pass")[ord(view.alias[-1]) - ord("A")]
    else:
        station = "board"
    if station not in window.legal_stations:
        station = "board" if "board" in window.legal_stations else "hold"
    response = PlanControl(
        station=station,
        recipe=recipe,
        zone="any",
        handoff="none",
        yield_to="none",
        say="",
        note=f"Private board recipe {recipe}; carrying {carried or 'nothing'}.",
    )
    parsed = parse_plan(response.model_dump_json(), window.legal_stations, window.ally_aliases)
    if parsed != response:
        raise ValueError("source teacher response differs from its ordinary parser result")
    return parsed
