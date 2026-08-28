"""Fixture registrations for the tests still under `x/`.

The definitions graduated to `session/conftest.py` with the session machinery; pytest only
collects conftests on a test's own ancestor path, so the harness, channel, and e2e tests below
this directory keep finding the fixtures here until their trees graduate too.
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
