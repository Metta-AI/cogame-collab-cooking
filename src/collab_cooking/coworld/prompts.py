"""Episode-stable wording independent of kitchen and alias randomness."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TRAINING_VARIANTS = ("canonical", "plain", "numbered", "questions", "reordered")
HELDOUT_VARIANTS = ("heldout-briefing", "heldout-reference")
BANK_VERSION = "collab-cooking-rules-v1"


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
