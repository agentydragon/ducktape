"""Conversation-test fixtures.

The record is written and read through the session machinery, so the fixtures are defined with it
in `session/conftest.py`; this file registers them for the tests here.
"""

from haku.console.session.conftest import (
    allocator,
    chat_service,
    conversation_wakes,
    operator_id,
    recording_claims,
    session_store,
    session_wakes,
)
