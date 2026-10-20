"""Episode-stable wording independent of kitchen and alias randomness."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TRAINING_VARIANTS = ("canonical", "plain", "numbered", "questions", "reordered")
HELDOUT_VARIANTS = ("heldout-briefing", "heldout-reference")
BANK_VERSION = "collab-cooking-rules-v2"


class PromptSelection(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    variant: Literal[
        "canonical",
        "plain",
        "numbered",
        "questions",
        "reordered",
        "heldout-briefing",
        "heldout-reference",
        "mixed",
    ] = Field(default="canonical", alias="promptVariant")
    seed: int = Field(default=0, alias="promptSeed", strict=True, ge=0)

    @model_validator(mode="after")
    def explicit_mixed_seed(self) -> PromptSelection:
        if self.variant == "mixed" and "seed" not in self.model_fields_set:
            raise ValueError("mixed wording requires an explicit promptSeed")
        return self

    @property
    def selected(self) -> str:
        return TRAINING_VARIANTS[self.seed % len(TRAINING_VARIANTS)] if self.variant == "mixed" else self.variant

    def metadata(self, system: str, user: str) -> dict:
        return {
            "bank": BANK_VERSION,
            "variant": self.selected,
            "selection": self.variant,
            "seed": self.seed,
            "system_sha256": hashlib.sha256(system.encode()).hexdigest(),
            "user_sha256": hashlib.sha256(user.encode()).hexdigest(),
        }

    def introduction(self, alias: str, max_steps: int) -> str:
        introductions = {
            "canonical": f"You are {alias}, one of 4 cogs running a kitchen together for {max_steps} ticks.\n",
            "plain": f"Your name is {alias}. You and three other cogs run a kitchen together for {max_steps} ticks.\n",
            "numbered": f"1. You are {alias}.\n2. Four cogs run this kitchen together.\n3. The game lasts {max_steps} ticks.\n",
            "questions": f"Who are you? {alias}. What are you doing? Running a kitchen with three other cogs for {max_steps} ticks.\n",
            "reordered": f"For {max_steps} ticks, four cogs run a kitchen together. You are {alias}.\n",
            "heldout-briefing": f"Take the role of {alias} in a kitchen operated jointly by four cogs over {max_steps} ticks.\n",
            "heldout-reference": f"Kitchen reference\nYour identity: {alias}.\nStaff: four cogs working together.\nDuration: {max_steps} ticks.\n",
        }
        return introductions[self.selected]


CANONICAL_SELECTION = PromptSelection()


RULE_TEMPLATES = {
    "canonical": """Orders arrive on the board every 18 ticks and expire 50 ticks later. A dish is a chain:
fetch veg or meat -> chop it (3 uses) -> pot it (soup: chopped veg + chopped meat, 10 ticks,
burns 14 ticks after it is ready) or fry it (chopped veg, 8 ticks, burns after 11) or plate
chopped veg as a salad -> carry it to the pass and serve it against a live ticket -> the plate
comes back dirty and needs 3 uses at the sink.
YOU CAN CARRY EXACTLY ONE THING. Counters (the walls) hold one item each: put your item down
and a team-mate can pick it up. That is often faster than walking round.
Your score is the team's dishes served plus 0.01 times the dishes you personally delivered.
Maximize team dishes first; your deliveries break ties. No other activity scores.
""",
    "plain": """Tickets appear every 18 ticks and remain live for 50 ticks. Fetch veg or meat and chop it with 3 uses. Soup
combines chopped veg and chopped meat in a pot: cooking takes 10 ticks and it burns 14 ticks after becoming
ready. Fries use chopped veg in the fryer: 8 ticks to cook, burning after 11. Plate chopped veg to make salad.
Take a dish to the pass and serve it against a live ticket. The returned dirty plate needs 3 sink uses.
Carry just one thing at a time. Each wall counter also holds one item; leave yours there for a teammate to
collect. A handoff can be faster than walking round.
Your score is the team's dishes served plus 0.01 times the dishes you personally delivered.
Maximize team dishes first; your deliveries break ties. No other activity scores.
""",
    "numbered": """Kitchen workflow:
1. Tickets arrive every 18 ticks and expire 50 ticks later.
2. Fetch veg or meat; chop with 3 uses.
3. Soup: chopped veg + chopped meat, pot, 10 cooking ticks, burns 14 ticks after ready.
4. Fries: chopped veg, fryer, 8 cooking ticks, burns after 11.
5. Salad: plate chopped veg.
6. Carry a dish to the pass and serve a live ticket. Wash the returned dirty plate with 3 sink uses.
7. Carry exactly one thing. Each counter wall holds one item: put yours down for a teammate to pick up, often
saving a walk around.
Your score is the team's dishes served plus 0.01 times the dishes you personally delivered.
Maximize team dishes first; your deliveries break ties. No other activity scores.
""",
    "questions": """How long do tickets last? They arrive every 18 ticks and expire 50 ticks later. How do you prepare
ingredients? Fetch veg or meat, then chop with 3 uses. How do you make soup? Pot chopped veg with chopped
meat; it cooks in 10 ticks and burns 14 ticks after ready. Fries? Fry chopped veg for 8 ticks; it burns after
11. Salad? Plate chopped veg. How do you finish? Carry the dish to the pass and serve a live ticket, then wash
the returned dirty plate with 3 sink uses.
Can you carry several things? No, exactly one. Can teammates help carry? Wall counters hold one item each; put
yours down for someone else to collect. This often beats walking round.
Your score is the team's dishes served plus 0.01 times the dishes you personally delivered.
Maximize team dishes first; your deliveries break ties. No other activity scores.
""",
    "reordered": """Your score is the team's dishes served plus 0.01 times the dishes you personally delivered.
Maximize team dishes first; your deliveries break ties. No other activity scores.
Carry exactly one thing; each wall counter also holds one item. Set yours down and a teammate can collect it,
often faster than walking round. Tickets arrive every 18 ticks and expire 50 ticks later.
To serve, bring a dish to the pass against a live ticket. The returned plate is dirty; 3 sink uses clean it.
Dishes start with fetched veg or meat, chopped using 3 uses. Plate chopped veg for salad. Pot chopped veg +
chopped meat for soup: 10 ticks until ready, then 14 ticks until burning. Fry chopped veg for fries: 8 ticks
to cook, burns after 11.
""",
    "heldout-briefing": """Service briefing: the board gets a ticket every 18 ticks, expiring 50 ticks later. Fetch veg or meat; 3
chopping uses prepare it. A pot combines chopped veg and chopped meat into soup after 10 ticks, burning 14
ticks after ready. A fryer turns chopped veg into fries after 8 ticks, burning after 11. Salad is plated
chopped veg. Carry dishes to the pass for a live ticket; clean the returned dirty plate with 3 sink uses.
Handoff briefing: you carry exactly one thing. Counter walls each hold one item, so you can leave yours for
another cog to collect. This is often quicker than walking round.
Your score is the team's dishes served plus 0.01 times the dishes you personally delivered.
Maximize team dishes first; your deliveries break ties. No other activity scores.
""",
    "heldout-reference": """Ticket reference: arrival every 18 ticks; expiry 50 ticks later. Ingredient reference: fetch veg/meat, then 3
chopping uses. Soup: pot chopped veg + chopped meat; ready in 10 ticks; burns 14 ticks after ready. Fries: fry
chopped veg; ready in 8 ticks; burns after 11. Salad: plate chopped veg. Service: carry to pass, serve a live
ticket. Cleaning: returned dirty plate, 3 sink uses.
Capacity reference: exactly one carried item; one item per wall counter. Handoff: place an item on a counter
for a teammate to collect, often faster than walking round.
Your score is the team's dishes served plus 0.01 times the dishes you personally delivered.
Maximize team dishes first; your deliveries break ties. No other activity scores.
""",
}
