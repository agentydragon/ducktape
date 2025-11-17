import asyncio
from collections.abc import Iterator
from enum import StrEnum
from typing import Annotated, Literal

from fastmcp.client import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.server import add_resource_prefix, has_resource_prefix, remove_resource_prefix
from mcp import types as mcp_types
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, ConfigDict, Field

from adgn.mcp._shared.constants import RESOURCES_SUBSCRIPTIONS_INDEX_URI
from adgn.mcp._shared.types import SimpleOk
from adgn.mcp._shared.urls import ANY_URL
from adgn.mcp.compositor.server import Compositor, MountEvent
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP
from adgn.mcp.resources.types import ListSubscriptionSummary, ResourceEntry, SubscriptionsIndex, SubscriptionSummary
from adgn.mcp.snapshots import RunningServerEntry

## ResourceEntry moved to adgn.mcp.resources.types to avoid cycles


class ResourcesListArgs(BaseModel):
    server: str | None = Field(default=None, description="Filter by server name (optional)")
    uri_prefix: str | None = Field(default=None, description="Restrict to URIs starting with this prefix (optional)")
    model_config = ConfigDict(extra="forbid")


class ResourcesListResult(BaseModel):
    resources: list[ResourceEntry] = Field(
        description="Aggregated resources across servers (each item includes origin server)"
    )
    model_config = ConfigDict(extra="forbid")


class ResourceWindowInfo(BaseModel):
    start_offset: int = Field(description="Start byte offset used for this window")
    max_bytes: int = Field(description="Max bytes requested for this window (0 means unbounded)")
    model_config = ConfigDict(extra="forbid")


"""
Typed read result is defined after WindowedPart to avoid forward refs.
"""


class ResourcesReadArgs(BaseModel):
    server: str = Field(description="Origin MCP server name that owns the resource")
    uri: str = Field(description="Resource URI as reported by the origin server's list")
    start_offset: int = Field(default=0, ge=0, description="Start byte offset for windowed reads")
    max_bytes: int = Field(default=0, ge=0, description="Max bytes to return (0 means no limit)")
    model_config = ConfigDict(extra="forbid")


# No compositor meta resources here; see adgn.mcp.compositor_meta.server

# ---- Top-level types for resources server (was nested) --------------------


class SubscriptionRecord(BaseModel):
    server: str
    uri: str
    pinned: bool = False
    active: bool = False
    last_error: str | None = None
    model_config = ConfigDict(extra="forbid")


class ListSubscribeArgs(BaseModel):
    server: str
    model_config = ConfigDict(extra="forbid")


class ResourceCapabilityFeature(StrEnum):
    SUBSCRIBE = "subscribe"
    LIST_CHANGED = "listChanged"


# ---- Internal typed representations for normalized/window parts -----------


class _MimeBase(BaseModel):
    mime: str | None = None
    model_config = ConfigDict(extra="forbid")


class TextPart(_MimeBase):
    kind: Literal["text"] = "text"
    raw_bytes: bytes


class BlobPart(_MimeBase):
    kind: Literal["base64"] = "base64"
    raw_str: str


NormalizedPart = Annotated[TextPart | BlobPart, Field(discriminator="kind")]


class _WindowedBase(_MimeBase):
    total_bytes: int
    bytes_returned: int


class WindowedTextPart(_WindowedBase):
    kind: Literal["text"] = "text"
    text: str


class WindowedBlobPart(_WindowedBase):
    kind: Literal["base64"] = "base64"
    base64: str


WindowedPart = Annotated[WindowedTextPart | WindowedBlobPart, Field(discriminator="kind")]


class ResourceReadResult(BaseModel):
    window: ResourceWindowInfo = Field(description="Windowing parameters reflected back")
    parts: list[WindowedPart] = Field(description="Windowed parts (text/base64)")
    total_parts: int = Field(description="Total number of parts reported by the origin server")
    model_config = ConfigDict(extra="forbid")


def _normalize_parts(
    contents: list[mcp_types.TextResourceContents | mcp_types.BlobResourceContents],
) -> list[NormalizedPart]:
    """Normalize resource parts to a small, typed internal shape."""
    norm: list[NormalizedPart] = []
    for p in contents:
        if isinstance(p, mcp_types.TextResourceContents):
            norm.append(TextPart(mime=p.mimeType, raw_bytes=p.text.encode("utf-8")))
            continue
        if isinstance(p, mcp_types.BlobResourceContents):
            base = p.blob  # base64 string per MCP spec
            norm.append(BlobPart(mime=p.mimeType, raw_str=str(base)))
            continue
        raise TypeError(f"Unsupported resource content type: {type(p).__name__}")
    return norm


def _iter_window_parts(
    contents: list[mcp_types.TextResourceContents | mcp_types.BlobResourceContents],
    start_offset: int,
    max_bytes: int | None,
) -> Iterator[WindowedPart]:
    remaining: int | None = max_bytes if isinstance(max_bytes, int) and max_bytes > 0 else None
    cursor = 0
    for part in _normalize_parts(contents):
        mime = part.mime
        if isinstance(part, TextPart):
            raw: bytes = part.raw_bytes
            total_len = len(raw)
            if remaining is None or remaining > 0:
                start_in_part = max(0, start_offset - cursor)
                take_cap = remaining if isinstance(remaining, int) else total_len
                take = max(0, min(take_cap, total_len - start_in_part))
                if take > 0:
                    chunk = raw[start_in_part : start_in_part + take]
                    yield WindowedTextPart(
                        mime=mime,
                        text=chunk.decode("utf-8", errors="replace"),
                        total_bytes=total_len,
                        bytes_returned=take,
                    )
                    if remaining is not None:
                        remaining -= take
            cursor += total_len
        elif isinstance(part, BlobPart):
            base: str = part.raw_str
            total_len = len(base)
            if remaining is None or remaining > 0:
                start_in_part = max(0, start_offset - cursor)
                take_cap = remaining if isinstance(remaining, int) else total_len
                take = max(0, min(take_cap, total_len - start_in_part))
                if take > 0:
                    yield WindowedBlobPart(
                        mime=mime,
                        base64=base[start_in_part : start_in_part + take],
                        total_bytes=total_len,
                        bytes_returned=take,
                    )
                    if remaining is not None:
                        remaining -= take
            cursor += total_len
        if remaining is not None and remaining <= 0:
            break


def _build_window_payload(
    contents: list[mcp_types.TextResourceContents | mcp_types.BlobResourceContents],
    start_offset: int,
    max_bytes: int | None,
) -> ResourceReadResult:
    parts_out: list[WindowedPart] = list(_iter_window_parts(contents, start_offset, max_bytes))
    return ResourceReadResult(
        window=ResourceWindowInfo(start_offset=start_offset, max_bytes=max_bytes or 0),
        parts=parts_out,
        total_parts=len(contents),
    )


def make_resources_server(
    name: str = "resources", *, gateway_client: Client, compositor: Compositor
) -> NotifyingFastMCP:
    """Create a MCP server that aggregates resources across servers.

    - Synthetic server injected by the runtime; reserved name is ``resources``.
    - Provides a uniform API to discover and read resources exposed by other servers.

    Tools
    - ``list(server?: string, uri_prefix?: string) -> { resources: [...] }``
      Server-side filters by server name and URI prefix.
    - ``read(server: string, uri: string, start_offset?: int = 0, max_bytes?: int)``
      Returns a windowed payload for large text/base64 resources.
    - TODO: ``list_resource_templates(server?: string)`` — expose origin templates via tool surface for LLMs.

    Window semantics
    - Windowing is byte-based across the concatenation of all parts reported by the
      underlying server. Text is sliced by UTF-8 bytes and decoded with
      ``errors="replace"`` if a multi-byte character is split at the boundary.
    - Base64 parts are sliced as base64 text; decoding is the caller's responsibility.

    Capability gating
    - Only servers that advertise ``initialize.capabilities.resources`` are queried.
    """
    mcp = NotifyingFastMCP(
        name, instructions=("Resources aggregator for listing/reading resources across mounted servers.")
    )

    # ---- Subscriptions index (single resource) -----------------------------
    # Internal store for subscriptions made via this server's subscribe tool.
    # No principals for now; keys are (server, uri).
    subs_lock = asyncio.Lock()
    subs: dict[tuple[str, str], SubscriptionRecord] = {}
    # Selected origins for list-changed subscriptions (multi-origin)
    list_subscribed_servers: set[str] = set()

    async def _broadcast_subs_updated() -> None:
        await mcp.broadcast_resource_updated(RESOURCES_SUBSCRIPTIONS_INDEX_URI)

    async def _present_servers() -> set[str]:
        # Include all mounted servers, including in-proc mounts without typed specs.
        # Use compositor._mount_names() directly; do not swallow errors.
        names = await compositor._mount_names()
        return set(names)

    def _get_or_create_sub(server: str, uri: str) -> SubscriptionRecord:
        key = (server, uri)
        rec = subs.get(key)
        if rec is None:
            rec = SubscriptionRecord(server=server, uri=uri)
            subs[key] = rec
        return rec

    async def _require_running_entry(server: str) -> RunningServerEntry:
        """Fetch the running server entry for a mounted server or raise a ToolError.

        This uses the Compositor's typed entries to ensure we have the
        InitializeResult for capabilities checks.
        """
        entries = await compositor.server_entries()
        entry = entries.get(server)
        if entry is None:
            raise ToolError(f"Unknown server '{server}'")
        if not isinstance(entry, RunningServerEntry):
            raise ToolError(f"Server '{server}' is not running (state={entry.state})")
        return entry

    async def _ensure_capability(server: str, *, feature: ResourceCapabilityFeature) -> None:
        """Ensure the target server advertises a required resources capability.

        Supported feature values:
        - ResourceCapabilityFeature.SUBSCRIBE: requires initialize.capabilities.resources.subscribe is True
        - ResourceCapabilityFeature.LIST_CHANGED: requires initialize.capabilities.resources.listChanged is True
        """
        entry = await _require_running_entry(server)
        try:
            caps = entry.initialize.capabilities
            res_caps = caps.resources
        except AttributeError as e:
            raise ToolError(f"Server '{server}' does not advertise resources capabilities") from e

        if res_caps is None:
            raise ToolError(f"Server '{server}' does not advertise resources capabilities")

        if feature is ResourceCapabilityFeature.SUBSCRIBE:
            ok = bool(res_caps.subscribe)
            needed = "resources.subscribe"
        elif feature is ResourceCapabilityFeature.LIST_CHANGED:
            ok = bool(res_caps.listChanged)
            needed = "resources.listChanged"
        else:
            raise ToolError(f"Unknown capability feature: {feature}")

        if not ok:
            raise ToolError(f"Server '{server}' does not support {needed}")

    @mcp.resource(
        RESOURCES_SUBSCRIPTIONS_INDEX_URI,
        name="resources.subscriptions",
        mime_type="application/json",
        description=("Index of resource subscriptions made via the resources server."),
    )
    async def subscriptions_index() -> SubscriptionsIndex:
        present = await _present_servers()
        async with subs_lock:
            items = list(subs.values())
            lss = set(list_subscribed_servers)
        out = [
            SubscriptionSummary(
                server=rec.server,
                uri=rec.uri,
                pinned=rec.pinned,
                present=(rec.server in present),
                active=rec.active and (rec.server in present),
                last_error=rec.last_error,
            )
            for rec in items
        ]
        list_out: list[ListSubscriptionSummary] = [
            ListSubscriptionSummary(server=s, present=(s in present), active=(s in present)) for s in sorted(lss)
        ]
        return SubscriptionsIndex(subscriptions=out, list_subscriptions=list_out)

    # TODO: Consider adding tools to subscribe/unsubscribe to resource list changes
    # (notifications/resources/list_changed). This would let the agent opt-in to
    # server-level list change notifications in addition to per-resource updates.
    # Design questions:
    # - Scope (global vs per-origin server)
    # - Persistence/lifetime (session-bound vs durable)
    # - Whether to expose a "refresh/list" tool to fetch the latest list on notify

    @mcp.flat_model()
    async def list_resources_tool(input: ResourcesListArgs) -> ResourcesListResult:
        """List resources via aggregator; derive origin using FastMCP prefix logic."""
        mcp_list = await gateway_client.list_resources()
        specs = await compositor.mount_specs()
        mount_names = sorted(specs.keys())
        out: list[ResourceEntry] = []
        for r in mcp_list:
            uri_str = str(r.uri)
            origin: str | None = None
            for mn in mount_names:
                if has_resource_prefix(uri_str, mn, compositor.resource_prefix_format):
                    origin = mn
                    break
            if input.server and origin != input.server:
                continue
            # If a uri_prefix filter is provided, match against the raw (de-prefixed) URI
            if input.uri_prefix and origin:
                raw_uri = remove_resource_prefix(uri_str, origin, compositor.resource_prefix_format)
                if not raw_uri.startswith(input.uri_prefix):
                    continue
            if origin is None:
                continue
            out.append(ResourceEntry(server=origin, resource=r))
        return ResourcesListResult(resources=out)

    @mcp.flat_model()
    async def read(input: ResourcesReadArgs) -> ResourceReadResult:
        """Read a resource with optional windowing (text/base64).

        Windowing semantics:
        - Byte-based across all parts reported by the origin server.
        - Set max_bytes to limit returned bytes (0 means unbounded).
        - For large content, use a chunk size (e.g., 16-64 KiB) and call again with
          start_offset advanced by the bytes_returned of the previous window.
        """
        prefixed = add_resource_prefix(input.uri, input.server, compositor.resource_prefix_format)
        uri_value = ANY_URL.validate_python(prefixed)
        res = await gateway_client.read_resource_mcp(uri_value)
        contents = list(res.contents)
        return _build_window_payload(contents, input.start_offset, None if input.max_bytes == 0 else input.max_bytes)

    @mcp.flat_model()
    async def subscribe(input: ResourcesReadArgs) -> SimpleOk:
        """Subscribe to updates for a resource."""
        await _ensure_capability(input.server, feature=ResourceCapabilityFeature.SUBSCRIBE)
        prefixed = add_resource_prefix(input.uri, input.server, compositor.resource_prefix_format)
        uri_value = ANY_URL.validate_python(prefixed)
        # Attempt subscribe; reflect success/error in index and re-raise on error.
        try:
            # Use the child's persistent session directly (already connected)
            child_client = compositor.get_child_client(input.server)
            await child_client.session.subscribe_resource(uri_value)
        except McpError as e:
            async with subs_lock:
                rec = _get_or_create_sub(input.server, input.uri)
                rec.active = False
                rec.last_error = f"{type(e).__name__}: {e}"
            await _broadcast_subs_updated()
            # Do not degrade on missing method; capability check should prevent reaching here
            raise
        else:
            async with subs_lock:
                rec = _get_or_create_sub(input.server, input.uri)
                rec.active = True
                rec.last_error = None
            await _broadcast_subs_updated()
            return SimpleOk(ok=True)

    @mcp.flat_model()
    async def unsubscribe(input: ResourcesReadArgs) -> SimpleOk:
        """Unsubscribe from updates for a resource."""
        await _ensure_capability(input.server, feature=ResourceCapabilityFeature.SUBSCRIBE)
        prefixed = add_resource_prefix(input.uri, input.server, compositor.resource_prefix_format)
        uri_value = ANY_URL.validate_python(prefixed)
        rec_key = (input.server, input.uri)
        try:
            child_client = compositor.get_child_client(input.server)
            await child_client.session.unsubscribe_resource(uri_value)
        except McpError as e:
            # Reflect error in index and re-raise
            async with subs_lock:
                if (rec := subs.get(rec_key)) is not None:
                    rec.active = False
                    rec.last_error = f"{type(e).__name__}: {e}"
            await _broadcast_subs_updated()
            # Do not degrade on missing method; capability check should prevent reaching here
            raise
        else:
            # Remove record entirely on explicit unsubscribe (no pin semantics yet)
            async with subs_lock:
                subs.pop(rec_key, None)
            await _broadcast_subs_updated()
            return SimpleOk(ok=True)

    @mcp.flat_model(name="subscribe_list_changes")
    async def subscribe_list_changes(input: ListSubscribeArgs) -> SimpleOk:
        """Subscribe to resources/list_changed for a single origin server.

        Multiple origins may be selected; repeated calls add servers to the selection.
        """
        await _ensure_capability(input.server, feature=ResourceCapabilityFeature.LIST_CHANGED)
        async with subs_lock:
            list_subscribed_servers.add(input.server)
        await _broadcast_subs_updated()
        return SimpleOk(ok=True)

    @mcp.flat_model(name="unsubscribe_list_changes")
    async def unsubscribe_list_changes(input: ListSubscribeArgs) -> SimpleOk:
        """Remove an origin from the list-changed subscription set (no-op if absent)."""
        async with subs_lock:
            list_subscribed_servers.discard(input.server)
        await _broadcast_subs_updated()
        return SimpleOk(ok=True)

    # Respond to Compositor lifecycle to keep index correct: tear down underlying
    # subscriptions on unmount, do not auto-rehydrate on mount. Non-pinned subs are
    # dropped; pinned kept inactive (no pin controls yet — placeholder for future).
    async def _on_mount_change(name: str, action: MountEvent) -> None:
        if action is not MountEvent.UNMOUNTED:
            return
        # Server is being unmounted. Do not attempt remote unsubscriptions; the
        # Compositor tears down underlying sessions. Update local records only.
        # Drop non-pinned entries for this server; mark pinned (future) inactive.
        async with subs_lock:
            to_delete = [(server, uri) for (server, uri), rec in subs.items() if server == name and not rec.pinned]
            for (server, uri), rec in list(subs.items()):
                if server == name and rec.pinned:
                    rec.active = False
                    subs[(server, uri)] = rec
            changed = bool(to_delete)
            for key in to_delete:
                subs.pop(key, None)
            # Drop list-changed selection for this origin if it is unmounted
            if name in list_subscribed_servers:
                list_subscribed_servers.discard(name)
                changed = True
        if changed:
            await _broadcast_subs_updated()

    compositor.add_mount_listener(_on_mount_change)

    # React to compositor list-changed notifications and reflect updates to the index
    async def _on_list_changed(name: str) -> None:
        async with subs_lock:
            subscribed = set(list_subscribed_servers)
        if name in subscribed:
            await _broadcast_subs_updated()

    compositor.add_list_changed_listener(_on_list_changed)

    # React to compositor resource-updated notifications: if a subscribed
    # resource (server, uri) matches, broadcast index update so UIs refresh.
    async def _on_resource_updated(name: str, uri: str) -> None:
        key = (name, uri)
        async with subs_lock:
            rec = subs.get(key)
            is_active = bool(rec and rec.active)
        if is_active:
            await _broadcast_subs_updated()

    compositor.add_resource_updated_listener(_on_resource_updated)

    # TODO: Consider per-subscription resources like
    #   resources://subscriptions/{server}/{percent-encoded-uri}
    # to enable list_changed semantics. For now, a single index resource is enough.

    return mcp
