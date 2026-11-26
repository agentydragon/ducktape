"""Register the real-browser fixtures; `sandbox` and `service_history_db_url` here outrank the app
conftest's, so the browser suites' Sandbox is the `ReplicationSource`'s and History reads Electric's
database."""

from agentplane.app.testing.thread_browser import (
    certificate,
    electric,
    page,
    replay_after,
    thread_browser,
    thread_browser_postgres,
    thread_browser_session,
    thread_source,
    viewport,
)
from agentplane.app.testing.thread_browser_backends import review_view, sandbox, service_history_db_url
