"""Reads of what the fold assembled: a command's archived admission answers its retry exactly."""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_bazel

from agentplane.app.agent_runtime.events.event_log import EventLogStore, ThreadNotFoundError
from agentplane.app.agent_runtime.events.ingestion_lease import IngestionLease
from agentplane.app.agent_runtime.view.content import CommandIdConflictError, ContentStore
from agentplane.app.conftest import SPEC, event_entry
from agentplane.app.ingestion import Ingestion
from agentplane.protocol import command_pb2, event_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def test_archived_command_admission_is_an_exact_retry_key(
    event_logs: EventLogStore, content: ContentStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    command = command_pb2.Command(
        command_id="submit-1", submit_input=command_pb2.SubmitInput(text="persist this exact input")
    )
    admitted = event_entry(1, command_admitted=event_pb2.CommandAdmitted(command=command))
    await ingestion.record(thread, [admitted], lease=lease)

    # The saved runner Event is the retry receipt, including its runner origin/cursor; it is not
    # rebuilt from a separate app command row.
    assert await content.admitted_command(thread, command) == admitted
    with pytest.raises(CommandIdConflictError, match="different work"):
        await content.admitted_command(
            thread, command_pb2.Command(command_id="submit-1", submit_input=command_pb2.SubmitInput(text="other input"))
        )
    with pytest.raises(ThreadNotFoundError):
        await content.admitted_command(UUID(int=0), command)


if __name__ == "__main__":
    pytest_bazel.main()
