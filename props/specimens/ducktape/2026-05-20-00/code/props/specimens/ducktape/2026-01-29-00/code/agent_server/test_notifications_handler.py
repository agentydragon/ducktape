from __future__ import annotations

import pytest_bazel

from agent_core.loop_control import InjectItems, NoAction
from agent_server.notifications.handler import NotificationsHandler
from agent_server.testing.helpers import strip_system_notification_wrapper
from mcp_infra.notifications.types import NotificationsBatch, ResourcesServerNotice
from openai_utils.text_extraction import extract_input_text_content


class _FakeBuffer:
    def __init__(self) -> None:
        # NotificationsBatch uses dict[server, ResourcesServerNotice]
        self._batch = NotificationsBatch(
            resources={
                "git-ro": ResourcesServerNotice(updated=frozenset({"http://a.txt"})),
                "editor": ResourcesServerNotice(updated=frozenset({"file:///b.py"})),
            }
        )

    def poll(self) -> NotificationsBatch:
        b = self._batch
        # Return once, then empty
        self._batch = NotificationsBatch()
        return b


def test_notifications_handler_batches_single_message():
    buf = _FakeBuffer()
    h = NotificationsHandler(buf.poll)
    dec = h.on_before_sample()
    assert isinstance(dec, InjectItems)
    assert len(dec.items) == 1
    msg = dec.items[0]
    # Extract input_text content from the message
    texts = extract_input_text_content([msg])
    assert texts, "expected an input_text content part"
    text = texts[0]
    # Strip system notification wrapper if present
    text = strip_system_notification_wrapper(text)
    # Simple repr-snippet assertion: ensure server names are present
    assert "git-ro" in text
    assert "editor" in text
    # Second call returns NoAction (empty)
    dec2 = h.on_before_sample()
    assert isinstance(dec2, NoAction)


if __name__ == "__main__":
    pytest_bazel.main()
