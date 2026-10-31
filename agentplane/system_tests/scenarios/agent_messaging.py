"""One agent messages another, which reads and acknowledges it, each from its own Sandbox.

The sender runs `agentplane notifications send` through the egress proxy, which puts the Pod's workload
identity on the request; the grant is the fixture's `send_grant`, a `session_inbox_access` rule to an inbox
tag the recipient carries, as staging's inbox groups are. The recipient learns of the message only from the
notice the platform delivers, and reads and acknowledges its inbox over HTTP, as the inbox-handling guide
tells an agent to.

What is checked is what the Notification Service recorded, never what either agent said: the entry names
the sender's ServiceAccount, which only the service's own authentication of the caller can put there, and the
sender's view of its message is covered but unacknowledged while the recipient holds the notice, and
acknowledged once the recipient acknowledged its inbox.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any
from uuid import UUID, uuid4

from google.protobuf.json_format import ParseDict
from google.protobuf.message import Message
from more_itertools import one
from pydantic import BaseModel

from agentplane.app.testing.client import Client
from agentplane.notification_service import notifications_pb2
from agentplane.proto import runner_pb2
from agentplane.system_tests import send_grant
from agentplane.system_tests.agent import Agent
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.sandbox import Sandboxes
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.tracing import tracer
from agentplane.testing.scripted_model import ModelRequest, ScriptedModel, ShellCall, Text

BODY = "Test message: the change is ready for review."
# What a notice's user message starts with, before its JSON (agentplane/notification_service/store/notices.go).
NOTICE_PREFIX = "Agentplane inbox notice: "
# The base URL and placeholder bearer the runner gives every Session (agentplane/docs/agent_cli.md).
NOTIFICATIONS = '"$AGENTPLANE_NOTIFICATIONS_URL"'
AUTHORIZATION = '-H "Authorization: Bearer $AGENTPLANE_NOTIFICATIONS_CREDENTIAL"'


class Notice(BaseModel):
    """The JSON a notice carries."""

    inbox_id: UUID
    acknowledged_at_preparation: int
    through_at_preparation: int


def _first_json(text: str) -> tuple[Any, int]:
    """The JSON value at the first `{` of `text`, and where it ends: a harness may wrap a command's output."""
    start = text.find("{")
    assert start >= 0, f"no JSON object in {text!r}"
    value, end = json.JSONDecoder().raw_decode(text, start)
    return value, end


def _parsed[M: Message](output: str, message: M) -> M:
    """A service's JSON answer, printed by a command in the Sandbox, as its message."""
    return ParseDict(_first_json(output)[0], message)


def _notice(request: ModelRequest[Any]) -> Notice:
    text = one(text for text in request.user_texts if NOTICE_PREFIX in text)
    start = text.index(NOTICE_PREFIX) + len(NOTICE_PREFIX)
    _, end = _first_json(text[start:])
    return Notice.model_validate_json(text[start : start + end])


def _output(request: ModelRequest[Any], call: ShellCall) -> str:
    return "\n".join(one(output for output in request.tool_outputs if output.call_id == call.call_id).blocks)


def _shell(command: str) -> ShellCall:
    return ShellCall(call_id=f"call-{uuid4().hex[:8]}", command=command)


async def _follow(agent: Agent, model: ScriptedModel[Any], message_id: str) -> notifications_pb2.Message:
    """The sender's own view of its message."""
    turn = await agent.run_shell(model, f"agentplane --json notifications message {message_id}")
    return _parsed(turn.tool_outputs[-1], notifications_pb2.Message())


async def an_agent_messages_another_which_reads_and_acknowledges_it(
    cluster: Cluster,
    client: Client,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
) -> None:
    async with asyncio.TaskGroup() as starting:
        recipient_started = starting.create_task(
            sandbox(f"systest-msg-to-{harness}", inbox_tags=[send_grant.INBOX_TAG])
        )
        sender_started = starting.create_task(sandbox(f"systest-msg-from-{harness}", policies=[send_grant.POLICY_SET]))
    recipient, sender = recipient_started.result(), sender_started.result()
    assert recipient.pod is not None
    assert sender.pod is not None
    recipient_model = scripted.model(recipient.pod.name, harness)
    sender_model = scripted.model(sender.pod.name, harness)
    recipient_agent = await Agent.open(client, sandbox=recipient.name, harness=harness, model=offered_model)
    sender_agent = await Agent.open(client, sandbox=sender.name, harness=harness, model=offered_model)

    send = (
        f"agentplane --json notifications send {cluster.namespace}/{recipient.name} {recipient_agent.thread_id}"
        f" --uid {recipient.uid} --key test-message-1 --body {shlex.quote(BODY)}"
    )
    with tracer.start_as_current_span(
        "system_test.agent_messaging.send", record_exception=False, set_status_on_exception=False
    ):
        sending = await sender_agent.run_shell(sender_model, send)
        sent = _parsed(sending.tool_outputs[-1], notifications_pb2.Message())
    assert sent.destination_ref.uid == str(recipient.uid), sending.transcript

    async with asyncio.TaskGroup() as turns:
        turns.create_task(recipient_agent.next_turn())
        with tracer.start_as_current_span(
            "system_test.agent_messaging.notice_wait", record_exception=False, set_status_on_exception=False
        ):
            notice_request = await recipient_model.request()
            notice = _notice(notice_request)

        with tracer.start_as_current_span(
            "system_test.agent_messaging.sender_state", record_exception=False, set_status_on_exception=False
        ):
            held = await _follow(sender_agent, sender_model, sent.id)
        assert held.covered, f"the recipient holds a notice, but the sender's message is not covered: {held}"
        assert not held.acknowledged, f"acknowledged before the recipient acknowledged anything: {held}"

        read = _shell(
            f"curl -sSf {AUTHORIZATION} {NOTIFICATIONS}'/v1/inboxes/{notice.inbox_id}/entries"
            f"?afterCursor={notice.acknowledged_at_preparation}&limit=128'"
        )
        with tracer.start_as_current_span(
            "system_test.agent_messaging.read_inbox", record_exception=False, set_status_on_exception=False
        ):
            await recipient_model.reply(notice_request, read)
            read_request = await recipient_model.request()
            inbox = _parsed(_output(read_request, read), notifications_pb2.ReadInboxResponse())
        assert inbox.inbox.session_id == str(recipient_agent.thread_id), inbox.inbox
        entry = one(entry for entry in inbox.entries if entry.event.message.message_id == sent.id)
        assert entry.event.message.sender == notifications_pb2.ServiceAccount(
            namespace=sender.service_account.namespace, name=sender.service_account.name
        ), entry
        assert entry.payload["body"] == BODY, entry
        assert entry.payload["from_session"] == str(sender_agent.thread_id), entry

        acknowledge = _shell(
            f"curl -sSf -X PUT {AUTHORIZATION} -H 'Content-Type: application/json'"
            f" -d '{{\"throughCursor\": {entry.cursor}}}' {NOTIFICATIONS}'/v1/inboxes/{notice.inbox_id}/acknowledgement'"
        )
        with tracer.start_as_current_span(
            "system_test.agent_messaging.acknowledge", record_exception=False, set_status_on_exception=False
        ):
            await recipient_model.reply(read_request, acknowledge)
            await recipient_model.reply(await recipient_model.request(), Text("Read and acknowledged."))

    with tracer.start_as_current_span(
        "system_test.agent_messaging.verify_sender_state", record_exception=False, set_status_on_exception=False
    ):
        followed = await _follow(sender_agent, sender_model, sent.id)
    assert followed.covered, followed
    assert followed.acknowledged, f"the recipient acknowledged its inbox through the message: {followed}"


# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf
