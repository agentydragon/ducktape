"""Hand-curated resolved tasks to bootstrap the gym before harvesting exists.

Outcomes were written from well-known public history; each carries an
`outcome_source` so it can be re-verified against the evidence checkout before
being used in anything load-bearing. Harvested task families (series-derived,
market-resolved) will replace this file's role; it stays as the smoke-test set.
"""

from __future__ import annotations

from datetime import date

from loom.gym.task import BinaryOutcome, BinaryQuestion, ScalarOutcome, ScalarQuestion, Task

SEED_TASKS: tuple[Task, ...] = (
    Task(
        task_id="sp500-close-6000-by-2024",
        as_of=date(2024, 7, 1),
        resolution_date=date(2024, 12, 31),
        question=BinaryQuestion(
            text="Will the S&P 500 index close at or above 6000 on any trading day on or before 2024-12-31?"
        ),
        outcome=BinaryOutcome(value=True),
        outcome_source="First close above 6000 was 2024-11-11 (6001.35); verify against the augur-evidence sp500 series.",
    ),
    Task(
        task_id="btc-100k-by-2024",
        as_of=date(2024, 7, 1),
        resolution_date=date(2024, 12, 31),
        question=BinaryQuestion(text="Will Bitcoin trade at or above $100,000 USD on or before 2024-12-31?"),
        outcome=BinaryOutcome(value=True),
        outcome_source="BTC first crossed $100k on 2024-12-05; verify against the augur-evidence crypto:BTC series.",
    ),
    Task(
        task_id="btc-100k-by-2023",
        as_of=date(2023, 1, 1),
        resolution_date=date(2023, 12, 31),
        question=BinaryQuestion(text="Will Bitcoin trade at or above $100,000 USD on or before 2023-12-31?"),
        outcome=BinaryOutcome(value=False),
        outcome_source="BTC ended 2023 around $42k, never near $100k; verify against the augur-evidence crypto:BTC series.",
    ),
    Task(
        task_id="openai-ipo-before-2025h2",
        as_of=date(2024, 7, 1),
        resolution_date=date(2025, 7, 1),
        question=BinaryQuestion(
            text="Will OpenAI complete an initial public offering (shares trading on a public exchange) before 2025-07-01?"
        ),
        outcome=BinaryOutcome(value=False),
        outcome_source="OpenAI remained private through 2025-07-01 (public funding-round record; see pm_reifier openai_history.json).",
    ),
    Task(
        task_id="sp500-close-2024-12-31",
        as_of=date(2024, 7, 1),
        resolution_date=date(2024, 12, 31),
        question=ScalarQuestion(
            text="What will the S&P 500 index closing level be on the last trading day of 2024?", unit="index points"
        ),
        outcome=ScalarOutcome(value=5881.63),
        outcome_source="^GSPC closed 2024-12-31 at 5881.63; verify against the augur-evidence sp500 series.",
    ),
    Task(
        task_id="us-cpi-yoy-2024-11",
        as_of=date(2024, 7, 1),
        resolution_date=date(2024, 12, 11),
        question=ScalarQuestion(
            text="What will US CPI-U year-over-year inflation be for November 2024, as first published by the BLS?",
            unit="percent",
        ),
        outcome=ScalarOutcome(value=2.7),
        outcome_source="BLS CPI-U YoY for Nov 2024 was 2.7% (released 2024-12-11); verify against FRED CPIAUCSL.",
    ),
)
