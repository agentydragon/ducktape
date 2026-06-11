from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from aiosqlite import Row
from fastmcp.resources import FunctionResource, ResourceTemplate
from pydantic import BaseModel, Field

from agent_server.persist.sqlite import SQLitePersistence
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.flat_tool import FlatTool
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# Mount prefixes (mounted in-proc with a shared store)
CHAT_HUMAN_MOUNT_PREFIX = MCPMountPrefix("chat_human")
CHAT_ASSISTANT_MOUNT_PREFIX = MCPMountPrefix("chat_assistant")


class ChatAuthor(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessage(BaseModel):
    id: int
    ts: datetime
    author: ChatAuthor
    mime: str = Field(default="text/markdown")
    content: str


def _row_to_message(row: Row) -> ChatMessage:
    # Accept Row objects (mapping interface with __getitem__ access)
    return ChatMessage(
        id=int(row["id"]),
        ts=datetime.fromisoformat(str(row["ts"])),
        author=ChatAuthor(str(row["author"])),
        mime=str(row["mime"]),
        content=str(row["content"]),
    )


class PostInput(OpenAIStrictModeBaseModel):
    mime: str = Field(description="MIME type for the content")
    content: str


class PostResult(BaseModel):
    id: int


class ReadPendingInput(OpenAIStrictModeBaseModel):
    limit: int | None = Field(ge=1, le=1000, description="Max messages to return (None = unlimited)")


class ReadPendingResult(BaseModel):
    messages: list[ChatMessage]
    last_id: int | None


@dataclass
class _ServerRefs:
    human: ChatServer | None = None
    assistant: ChatServer | None = None


class ChatStore:
    """In-memory chat store shared by chat.human and chat.assistant servers.

    - Monotonic integer sequence for ordering (message IDs)
    - Per-author last-read high-water mark (get+advance via read_pending_messages)
    - Broadcasts head updates on the other participant's server when a message arrives
    """

    def __init__(self) -> None:
        self._seq: int = 0
        self._messages: list[tuple[int, ChatMessage]] = []
        # Per-author last-read sequence (None when nothing read yet)
        self._last_read: dict[ChatAuthor, int | None] = {ChatAuthor.USER: None, ChatAuthor.ASSISTANT: None}
        self._servers = _ServerRefs()

    def register_servers(self, *, human: ChatServer | None, assistant: ChatServer | None) -> None:
        self._servers = _ServerRefs(human=human, assistant=assistant)

    async def last_id_async(self) -> int | None:
        return self._seq if self._seq > 0 else None

    async def get_last_read(self, author: ChatAuthor) -> int | None:
        return self._last_read[author]

    async def _notify_other_head(self, *, author: ChatAuthor) -> None:
        if author is ChatAuthor.USER and self._servers.assistant is not None:
            await self._servers.assistant.broadcast_resource_updated(self._servers.assistant.head_resource.uri)
        elif author is ChatAuthor.ASSISTANT and self._servers.human is not None:
            await self._servers.human.broadcast_resource_updated(self._servers.human.head_resource.uri)

    async def _notify_last_read(self, *, author: ChatAuthor) -> None:
        srv = self._servers.human if author is ChatAuthor.USER else self._servers.assistant
        if srv is not None:
            await srv.broadcast_resource_updated(srv.last_read_resource.uri)

    async def append(self, *, author: ChatAuthor, mime: str, content: str) -> int:
        self._seq += 1
        seq = self._seq
        msg = ChatMessage(id=seq, ts=datetime.now(UTC), author=author, mime=mime, content=content)
        self._messages.append((seq, msg))

        # Notify the other participant's head resource
        await self._notify_other_head(author=author)
        return msg.id

    async def get_message_async(self, msg_id: int) -> ChatMessage | None:
        for s, m in self._messages:
            if s == msg_id:
                return m
        return None

    def _read_since_seq(
        self, *, other_author: ChatAuthor, after_seq: int | None, limit: int | None
    ) -> list[ChatMessage]:
        out: list[ChatMessage] = []
        cap = limit if isinstance(limit, int) and limit > 0 else None
        for s, m in self._messages:
            if after_seq is not None and s <= after_seq:
                continue
            if m.author != other_author:
                continue
            out.append(m)
            if cap is not None and len(out) >= cap:
                break
        return out

    async def read_pending_and_advance(
        self, *, author: ChatAuthor, limit: int | None
    ) -> tuple[list[ChatMessage], int | None]:
        other = ChatAuthor.ASSISTANT if author is ChatAuthor.USER else ChatAuthor.USER
        after_seq = self._last_read[author]
        msgs = self._read_since_seq(other_author=other, after_seq=after_seq, limit=limit)
        if msgs:
            # Advance last-read to last message seq and notify
            self._last_read[author] = msgs[-1].id
            await self._notify_last_read(author=author)
        return msgs, await self.last_id_async()


class ChatStorePersisted(ChatStore):
    """SQLite-backed ChatStore bound to an agent_id.

    Uses SQLitePersistence directly; tables are created via ensure_schema().
    """

    def __init__(self, *, persistence: SQLitePersistence, agent_id: str) -> None:
        super().__init__()
        self._persistence = persistence
        self._agent = agent_id

    async def _fetch_optional_int(self, query: str, params: tuple) -> int | None:
        """Execute query and return optional int from first row, first column."""
        async with self._persistence._open_row() as db, db.execute(query, params) as cur:
            if (row := await cur.fetchone()) and (val := row[0]) is not None:
                return int(val)
            return None

    async def last_id_async(self) -> int | None:
        return await self._fetch_optional_int("SELECT MAX(id) FROM chat_messages WHERE agent_id = ?", (self._agent,))

    async def get_last_read(self, author: ChatAuthor) -> int | None:
        return await self._fetch_optional_int(
            "SELECT last_id FROM chat_last_read WHERE agent_id = ? AND server_name = ?", (self._agent, author.value)
        )

    async def append(self, *, author: ChatAuthor, mime: str, content: str) -> int:
        ts = datetime.now(UTC).isoformat()
        async with self._persistence._open() as db:
            cur = await db.execute(
                "INSERT INTO chat_messages (agent_id, ts, author, mime, content) VALUES (?, ?, ?, ?, ?)",
                (self._agent, ts, author.value, mime, content),
            )
            await db.commit()
            new_id = cur.lastrowid
        # Notify other participant
        await self._notify_other_head(author=author)
        assert new_id is not None, "lastrowid should be set after INSERT"
        return int(new_id)

    async def get_message_async(self, msg_id: int) -> ChatMessage | None:
        async with (
            self._persistence._open_row() as db,
            db.execute(
                "SELECT id, ts, author, mime, content FROM chat_messages WHERE agent_id = ? AND id = ?",
                (self._agent, msg_id),
            ) as cur,
        ):
            if not (row := await cur.fetchone()):
                return None
            return _row_to_message(row)

    def _read_since_seq(
        self, *, other_author: ChatAuthor, after_seq: int | None, limit: int | None
    ) -> list[ChatMessage]:
        # Not used in persisted path
        raise NotImplementedError

    async def read_pending_and_advance(
        self, *, author: ChatAuthor, limit: int | None
    ) -> tuple[list[ChatMessage], int | None]:
        other = ChatAuthor.ASSISTANT if author is ChatAuthor.USER else ChatAuthor.USER
        cap = limit if isinstance(limit, int) and limit > 0 else None

        # Get current read position
        after_seq = await self.get_last_read(author)

        # Fetch messages after HWM
        msgs: list[ChatMessage] = []
        async with self._persistence._open_row() as db:
            sql = (
                "SELECT id, ts, author, mime, content FROM chat_messages "
                "WHERE agent_id = ? AND id > ? AND author = ? ORDER BY id ASC"
            )
            params: list[object] = [self._agent, (after_seq or 0), other.value]
            if cap is not None:
                sql += " LIMIT ?"
                params.append(cap)
            async with db.execute(sql, tuple(params)) as cur:
                async for r in cur:
                    msgs.append(_row_to_message(r))

        if msgs:
            async with self._persistence._open() as db:
                await db.execute(
                    "INSERT INTO chat_last_read (agent_id, server_name, last_id) VALUES (?, ?, ?) "
                    "ON CONFLICT(agent_id, server_name) DO UPDATE SET last_id=excluded.last_id",
                    (self._agent, author.value, msgs[-1].id),
                )
                await db.commit()
            await self._notify_last_read(author=author)
        return msgs, await self.last_id_async()


class ChatServer(EnhancedFastMCP):
    """Chat MCP server with typed tool access.

    Provides bidirectional messaging between user and assistant with message tracking,
    notifications, and read position management.
    """

    # Resource attributes (stashed results of @resource decorator - single source of truth for URI access)
    head_resource: FunctionResource
    last_read_resource: FunctionResource
    message_resource: ResourceTemplate

    # Tool references (assigned in __init__)
    post_tool: FlatTool[Any, Any]
    read_pending_messages_tool: FlatTool[Any, Any]

    def __init__(self, *, author: ChatAuthor, store: ChatStore):
        """Create a chat server bound to a fixed author and a shared store.

        Args:
            author: Which side of the conversation this server represents (also used for tracking read positions)
            store: Shared store for messages and read positions
        """
        display = f"Chat MCP Server ({author.value})"
        super().__init__(display, instructions=None)

        # Head sentinel: last_id only (small)
        async def head() -> int | None:
            return await store.last_id_async()

        self.head_resource = cast(
            FunctionResource, self.resource("chat://head", name="chat.head", mime_type="application/json")(head)
        )

        # Last-read HWM (server-managed)
        async def last_read() -> int | None:
            return await store.get_last_read(author)

        self.last_read_resource = cast(
            FunctionResource,
            self.resource("chat://last-read", name="chat.last_read", mime_type="application/json")(last_read),
        )

        # Per-message resource for deep links/hydration
        async def message(id: int) -> ChatMessage:
            got = await store.get_message_async(id)
            if got is None:
                raise KeyError(str(id))
            return got

        self.message_resource = cast(
            ResourceTemplate,
            self.resource("chat://messages/{id}", name="chat.message", mime_type="application/json")(message),
        )

        # Tools: post and read_pending_messages (get+advance)
        async def post(input: PostInput) -> PostResult:
            new_id = await store.append(author=author, mime=input.mime, content=input.content)
            return PostResult(id=new_id)

        self.post_tool = self.flat_model()(post)

        async def read_pending_messages(input: ReadPendingInput) -> ReadPendingResult:
            msgs, last_id = await store.read_pending_and_advance(author=author, limit=input.limit)
            return ReadPendingResult(messages=msgs, last_id=last_id)

        self.read_pending_messages_tool = self.flat_model()(read_pending_messages)


async def attach_chat_servers(
    comp,
    *,
    human_prefix: MCPMountPrefix = CHAT_HUMAN_MOUNT_PREFIX,
    assistant_prefix: MCPMountPrefix = CHAT_ASSISTANT_MOUNT_PREFIX,
):
    """Attach chat.human and chat.assistant in-proc backed by a shared store.

    Returns (store, human_server, assistant_server).
    """
    store = ChatStore()
    human = ChatServer(author=ChatAuthor.USER, store=store)
    assistant = ChatServer(author=ChatAuthor.ASSISTANT, store=store)
    # Register servers for cross-broadcasts
    store.register_servers(human=human, assistant=assistant)
    await comp.mount_inproc(human_prefix, human)
    await comp.mount_inproc(assistant_prefix, assistant)
    return store, human, assistant


async def attach_persisted_chat_servers(
    comp,
    *,
    persistence: SQLitePersistence,
    agent_id: str,
    human_prefix: MCPMountPrefix = CHAT_HUMAN_MOUNT_PREFIX,
    assistant_prefix: MCPMountPrefix = CHAT_ASSISTANT_MOUNT_PREFIX,
):
    store = ChatStorePersisted(persistence=persistence, agent_id=agent_id)
    human = ChatServer(author=ChatAuthor.USER, store=store)
    assistant = ChatServer(author=ChatAuthor.ASSISTANT, store=store)
    store.register_servers(human=human, assistant=assistant)
    await comp.mount_inproc(human_prefix, human)
    await comp.mount_inproc(assistant_prefix, assistant)
    return store, human, assistant
