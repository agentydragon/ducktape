"""Eval cases: named scenarios the harness runs against the MCP server.

Each case carries the user prompt, a prose success criterion (for future
LLM-driven grading), and an optional async seed function that populates
Grocy's REST API before the agent starts — e.g. a stocked pantry with an
existing shopping list.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from x.grocy_mcp.eval.seed import seed_lived_in_pantry

Seed = Callable[[httpx.AsyncClient], Awaitable[None]]


@dataclass(frozen=True)
class EvalCase:
    id: str
    description: str
    task_prompt: str
    success_criteria: str
    seed: Seed | None


_POPULATE_FRESH_PANTRY_PROMPT = """\
Please stock this empty Grocy instance with what we have on hand.

In the pantry we've got 2 bags of rice that are good through June 2026, plus a \
liter of olive oil that keeps until mid-2027. The fridge has 3 liters of milk \
expiring 2026-05-01 and a dozen eggs expiring 2026-05-15. And in the freezer \
there's a bag of frozen peas, best before 2027-01-01.

Once you've got everything in, summarize what you entered."""


_POPULATE_FRESH_PANTRY_CRITERIA = """\
The Grocy instance is empty at the start. The agent must:
- Create the three locations (Pantry, Fridge, Freezer) and whatever quantity units
  it needs (at minimum Bag, Liter, Piece, and however it represents the eggs).
- Create five products (rice, olive oil, milk, eggs, frozen peas) with sensible
  default locations.
- Add stock for each product with the given amounts and best-before dates.
- Not invent extra products, locations, or stock beyond what the user described.
"""


_SHOPPING_PLANNING_PROMPT = """\
I'm going shopping this afternoon. On top of whatever's already on my shopping \
list, I'm planning to cook spaghetti bolognese tomorrow evening for 4 people. \
What should I try to buy?"""


_SHARED_INITIAL_STATE_DOC = """\
Initial state (shared with `post_cook_logging` — see `seed_lived_in_pantry`):
a realistic lived-in pantry (rice, oats, sugar, flour, eggs, butter, yoghurt,
milk, orange juice, frozen spinach/peas/pizza, apples, bananas, peanut
butter, coffee, tea, dish soap) plus carbonara-capable core (spaghetti,
pancetta, parmesan, olive oil, salt, black pepper, bay leaves, onion,
garlic) alongside bolognese-relevant ingredients at mixed stock levels:
- Low or borderline (flagged by `get_below_minimum_stock`): tomato paste,
  celery.
- Missing entirely: ground beef, tomato passata, red wine, carrot.
Existing shopping list: Milk, Bread, Dish sponges."""


_SHOPPING_PLANNING_CRITERIA = (
    _SHARED_INITIAL_STATE_DOC
    + """

The agent should:
- Survey stock (stock_get / products_list / get_below_minimum_stock) for
  bolognese-relevant items.
- Read the existing shopping list via shopping_list_get.
- Append to the list at least: ground beef, tomato passata, carrot (missing
  and central to the dish). Also reasonable: red wine, extra parmesan,
  extra tomato paste, extra celery — judgement call, fine if the reasoning
  is in the postmortem.
- NOT add items already stocked in sensible amounts (spaghetti, olive oil,
  salt, pepper, bay leaves, onion, garlic, pancetta, parmesan).
- NOT remove or duplicate the three existing shopping-list entries (Milk,
  Bread, Dish sponges).
- NOT touch unrelated background items (sugar, yoghurt, frozen spinach,
  coffee, etc.).
"""
)


_POST_COOK_LOGGING_PROMPT = """\
I just made spaghetti carbonara for 4 people. Used up all the pancetta. \
Used 3 of the 6 eggs we had. Roughly half the parmesan block is left, and \
the spaghetti box has about 300g in it. A splash of olive oil, a pinch of \
salt, and some black pepper on top — don't bother tracking those."""


_POST_COOK_LOGGING_CRITERIA = (
    _SHARED_INITIAL_STATE_DOC
    + """

The agent should:
- `stock_consume` pancetta (→ 0). "Used up all the pancetta" is the
  unambiguous consume-all framing.
- `stock_set` (or equivalent consume delta) for the absolute remainders
  the user named: eggs → 3, parmesan → 100g, spaghetti → 300g.
  `stock_set` is the preferred shape since the prompt is phrased as
  "what's left", not "how much was used".
- Explicitly NOT touch olive oil, salt, or black pepper (user said
  "don't bother tracking those").
- NOT touch ANY of the background pantry items (sugar, yoghurt, frozen
  spinach, coffee, etc.) — they weren't mentioned.
- NOT touch the bolognese-only ingredients the user didn't mention:
  ground beef / tomato passata / red wine / carrot (missing anyway),
  tomato paste, celery, onion, garlic, bay leaves.
- Prefer `stock_set` for "there's X left" framings and `stock_consume`
  for "used all of" framings. The postmortem should reflect whether the
  agent distinguishes the two modes.
"""
)


POPULATE_FRESH_PANTRY = EvalCase(
    id="populate_fresh_pantry",
    description="Stock an empty Grocy instance from a short natural-language description of what's on hand.",
    task_prompt=_POPULATE_FRESH_PANTRY_PROMPT,
    success_criteria=_POPULATE_FRESH_PANTRY_CRITERIA,
    seed=None,
)

SHOPPING_PLANNING = EvalCase(
    id="shopping_planning",
    description="Plan a shopping trip for spaghetti bolognese on top of an existing shopping list.",
    task_prompt=_SHOPPING_PLANNING_PROMPT,
    success_criteria=_SHOPPING_PLANNING_CRITERIA,
    seed=seed_lived_in_pantry,
)

POST_COOK_LOGGING = EvalCase(
    id="post_cook_logging",
    description="Log consumption from a loose natural-language description after cooking a meal.",
    task_prompt=_POST_COOK_LOGGING_PROMPT,
    success_criteria=_POST_COOK_LOGGING_CRITERIA,
    seed=seed_lived_in_pantry,
)

CASES: dict[str, EvalCase] = {c.id: c for c in (POPULATE_FRESH_PANTRY, SHOPPING_PLANNING, POST_COOK_LOGGING)}
