"""Classifying Claude's frames while no turn is open: idle chatter, or the CLI waking itself.

Claude Code resumes its own session to observe work it left running, and the shapes are pinned by
recorded captures (`testdata/background_wake.jsonl`, `testdata/scheduled_wakeup_fire.jsonl`):

- A background command completing: `system/background_tasks_changed`, `system/task_updated`, then
  `system/task_notification` carrying the human-readable summary — followed by `system/init`,
  `system/status`, and the exchange's first content frame.
- A `ScheduleWakeup` firing: `command_lifecycle` (`started`), then `init`, `status`, content. No
  frame carries prose about why.

Neither shape includes a `user` frame in current CLI builds — the injected command stays internal —
but an idle `user` frame with text content is classified as a wake anyway, with that text as the
description: it is the harness speaking either way, and dropping it would lose the one wake shape
that says the most.
"""

from __future__ import annotations

from dataclasses import dataclass

from haku.console.x.runtime import WakeStart
from haku.runner.protocol import HarnessFrame

# What a wake says when none of its frames said anything — the ScheduleWakeup shape.
GENERIC_WAKE_DESCRIPTION = "The session woke itself."


def _user_text(payload: dict[str, object]) -> str | None:
    """The text of a harness-injected user command, or None for a tool-result user frame."""
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    texts = [str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text"]
    return "\n".join(text for text in texts if text).strip() or None


@dataclass(slots=True)
class ClaudeWakeWatcher:
    """One idle span's accumulated view of what the CLI is doing.

    The announcement and the exchange are separate frames, so the watcher carries the best
    description seen so far and spends it on the frame that begins the exchange.
    """

    _description: str | None = None

    def observe(self, frame: HarnessFrame) -> WakeStart | None:
        payload = frame.frame
        kind = payload.get("type")
        if kind == "system":
            if payload.get("subtype") == "task_notification" and isinstance(payload.get("summary"), str):
                self._description = payload["summary"]
            return None
        if kind == "user":
            if (text := _user_text(payload)) is not None:
                return WakeStart(description=self._description or text)
            return None
        if kind in ("stream_event", "assistant"):
            return WakeStart(description=self._description or GENERIC_WAKE_DESCRIPTION)
        # `command_lifecycle`, control frames, `rate_limit_event`, a stray `result`: idle chatter.
        return None
