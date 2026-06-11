from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from fastmcp.client.messages import MessageHandler
from mcp import types
from pydantic import AnyUrl

from openai_utils.model import InputTextPart, UserMessage


class ResourceUpdatedCapture(MessageHandler):
    """MessageHandler that captures resource updated notifications."""

    def __init__(self) -> None:
        self.updated: list[AnyUrl] = []

    async def on_resource_updated(self, message: types.ResourceUpdatedNotification) -> None:
        self.updated.append(message.params.uri)


async def subscribe_head(session, chat_server) -> None:
    """Subscribe to chat head updates for the connected server."""
    await session.subscribe_resource(chat_server.head_resource.uri)


def _iter_text_parts(message: UserMessage) -> Iterable[str]:
    for part in message.content or []:
        if isinstance(part, InputTextPart) and part.text:
            yield part.text


def parse_system_notification_payload(message: str | UserMessage) -> dict:
    """Extract and parse the JSON payload in a tagged system notification message.

    Expects the message to contain:
      <system notification>\n{json}\n</system notification>
    Returns the parsed dict, or raises ValueError on malformed input.
    """
    if isinstance(message, UserMessage):
        parts = list(_iter_text_parts(message))
        if not parts:
            raise ValueError("Message has no text parts to inspect")
        text = "\n".join(parts)
    else:
        text = message

    start_tag = "<system notification>"
    end_tag = "</system notification>"
    start = text.find(start_tag)
    end = text.find(end_tag)
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Not a tagged system notification message")
    payload_str = text[start + len(start_tag) : end].strip()
    result = json.loads(payload_str)
    if not isinstance(result, dict):
        raise ValueError("Payload is not a JSON object")
    return result


def enable_resources_caps(server: Any, *, subscribe: bool | None = None, list_changed: bool | None = None) -> None:
    """Monkeypatch a FastMCP/EnhancedFastMCP server to advertise resources capabilities.

    This wraps the server's low-level create_initialization_options() to inject
    experimental_capabilities for the 'resources' group.
    Call this before mounting/connecting the server so the init handshake carries
    the desired caps.
    """
    mcp_server = getattr(server, "_mcp_server", None)
    if mcp_server is None:
        raise RuntimeError("Server has no _mcp_server to patch")
    base_create = mcp_server.create_initialization_options
    base_get_caps = mcp_server.get_capabilities

    def patched_create_initialization_options(
        notification_options: Any = None, experimental_capabilities: Any = None, **kwargs: Any
    ) -> Any:
        caps = dict(experimental_capabilities or {})
        res = dict(caps.get("resources") or {})
        if subscribe is not None:
            res["subscribe"] = subscribe
        if list_changed is not None:
            res["listChanged"] = list_changed
        if res:
            caps["resources"] = res
        return base_create(notification_options=notification_options, experimental_capabilities=caps, **kwargs)

    mcp_server.create_initialization_options = patched_create_initialization_options

    def patched_get_capabilities(notification_options: Any, experimental_capabilities: Any) -> types.ServerCapabilities:
        caps: types.ServerCapabilities = base_get_caps(notification_options, experimental_capabilities)
        res_caps = caps.resources
        if subscribe is not None or list_changed is not None:
            if res_caps is None:
                res_caps = types.ResourcesCapability()
            if subscribe is not None:
                res_caps.subscribe = subscribe
            if list_changed is not None:
                res_caps.listChanged = list_changed
            caps.resources = res_caps
        return caps

    mcp_server.get_capabilities = patched_get_capabilities

    if subscribe:
        # Ensure subscribe/unsubscribe handlers exist so requests succeed.
        if types.SubscribeRequest not in mcp_server.request_handlers:

            @mcp_server.subscribe_resource()
            async def _test_subscribe(_uri):
                return None

        if types.UnsubscribeRequest not in mcp_server.request_handlers:

            @mcp_server.unsubscribe_resource()
            async def _test_unsubscribe(_uri):
                return None


class SubscriptionRecorder:
    """Record subscribe/unsubscribe requests made to a server for test assertions."""

    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []


def install_subscription_recorder(server: Any) -> SubscriptionRecorder:
    """Register lightweight subscribe/unsubscribe handlers that record URIs."""
    mcp_server = getattr(server, "_mcp_server", None)
    if mcp_server is None:
        raise RuntimeError("Server has no _mcp_server to patch")
    recorder = SubscriptionRecorder()

    @mcp_server.subscribe_resource()
    async def _record_subscribe(uri: str) -> None:
        recorder.subscribed.append(str(uri))

    @mcp_server.unsubscribe_resource()
    async def _record_unsubscribe(uri: str) -> None:
        recorder.unsubscribed.append(str(uri))

    return recorder
