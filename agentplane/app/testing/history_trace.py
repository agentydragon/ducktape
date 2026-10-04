"""The thread history's flight recorder (frontend/threads/history_trace.ts), read back from a
Playwright page as typed events.

`HistoryEvent` there and the models here are one contract: an event kind or field added on either
side fails the parse of the other, loudly, rather than being dropped from a failure's dump."""

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

from playwright.async_api import Error as PlaywrightError, Page
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.alias_generators import to_camel

logger = logging.getLogger(__name__)


class _Recorded(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, extra="forbid", frozen=True)


class Follow(_Recorded):
    kind: Literal["follow"]
    following: bool
    reason: Literal[
        "jump-to-latest",
        "returned-to-previous-bottom",
        "scrolled-to-bottom",
        "scrolled-up",
        "wheel-up",
        "key-up",
        "touch-up",
        "disclosure-click",
    ]


class Scroll(_Recorded):
    kind: Literal["scroll"]
    scroll_top: float
    scroll_height: float
    followed: bool


class ScrollEnd(_Recorded):
    kind: Literal["scrollend"]
    restoring: bool
    capturing: bool


class Resize(_Recorded):
    kind: Literal["resize"]
    scroll_top: float
    scroll_height: float
    pinned: bool


class Click(_Recorded):
    kind: Literal["click"]
    scroll_top: float
    scroll_height: float
    client_height: float


class Anchor(_Recorded):
    kind: Literal["anchor"]
    key: str
    offset: float


class Restore(_Recorded):
    kind: Literal["restore"]
    key: str
    correction: float | None


class Prepend(_Recorded):
    kind: Literal["prepend"]
    added: int


class LoadOlder(_Recorded):
    kind: Literal["load-older"]


class Measure(_Recorded):
    kind: Literal["measure"]
    key: str
    estimate: float
    measured: float
    first: bool
    remembered: bool


class Settled(_Recorded):
    kind: Literal["settled"]
    settled: bool


HistoryEvent = Annotated[
    Follow | Scroll | ScrollEnd | Resize | Click | Anchor | Restore | Prepend | LoadOlder | Measure | Settled,
    Field(discriminator="kind"),
]


class TimedHistoryEvent(_Recorded):
    at: float = Field(description="`performance.now()` in the page, in milliseconds.")
    event: HistoryEvent


class EstimateError(_Recorded):
    error: float = Field(description="`measured - estimate` of a row's first reading, in pixels.")
    remembered: bool = Field(description="Whether the estimate was an earlier visit's reading, not the flat guess.")


_EVENTS = TypeAdapter(list[TimedHistoryEvent])
_ESTIMATE_ERRORS = TypeAdapter(list[EstimateError])


async def events(page: Page, since: float = 0.0) -> list[TimedHistoryEvent]:
    """The history's recorded decisions at or after `since`, a `performance.now()` reading."""
    return _EVENTS.validate_python(
        await page.evaluate(
            "since => (window.agentplaneHistoryTrace?.() ?? []).filter(entry => entry.at >= since)", since
        )
    )


async def estimate_errors(page: Page) -> list[EstimateError]:
    return _ESTIMATE_ERRORS.validate_python(
        await page.evaluate("() => window.agentplaneHistoryEstimateErrors?.() ?? []")
    )


def as_json_lines(recorded: Sequence[TimedHistoryEvent]) -> str:
    """One line per event, in the names the page's own `agentplaneHistoryTrace()` shows."""
    return "\n".join(entry.model_dump_json(by_alias=True) for entry in recorded)


async def recent(page: Page, count: int) -> str:
    """The last `count` recorded events, for a failure's message."""
    return as_json_lines((await events(page))[-count:])


async def write(page: Page, path: Path) -> None:
    """Everything the page recorded, as JSON lines, for a failure that left no message to carry it."""
    try:
        recorded = await events(page)
    except PlaywrightError:
        logger.warning("the history's recorder could not be read from the page", exc_info=True)
        return
    await asyncio.to_thread(path.write_text, as_json_lines(recorded))
