"""The writer tests bind pushed Sessions to."""

from agentplane.history_service.storage.store import Writer

WRITER = Writer(namespace="test-sandbox-namespace", name="test-sandbox-runner", uid="test-runner-account-uid")
