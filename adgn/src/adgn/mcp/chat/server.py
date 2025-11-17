from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from aiosqlite import Row
from pydantic import BaseModel, Field

from adgn.agent.persist.sqlite import SQLitePersistence
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP

# Server names (mounted in-proc with a shared store)
CHAT_HUMAN_SERVER_NAME = "chat.human"
CHAT_ASSISTANT_SERVER_NAME = "chat.assistant"


class ChatAuthor(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessage(BaseModel):
    id: str
    ts: str
    author: ChatAuthor
    mime: str = Field(default="text/markdown")
    content: str


def _row_to_message(row: Row) -> ChatMessage:
    # Accept Row objects (mapping interface with __getitem__ access)
    return ChatMessage(
        id=str(row["id"]),
        ts=str(row["ts"]),
        author=ChatAuthor(str(row["author"])),
        mime=str(row["mime"]),
        content=str(row["content"]),
    )


class PostInput(BaseModel):
    mime: str = Field(default="text/markdown")
    content: str


class PostResult(BaseModel):
    id: str


class ReadPendingInput(BaseModel):
    limit: int | None = Field(default=50, ge=1, le=1000)


class ReadPendingResult(BaseModel):
    messages: list[ChatMessage]
    last_id: str | None


@dataclass
class _ServerRefs:
    human: NotifyingFastMCP | None = None
    assistant: NotifyingFastMCP | None = None


class ChatStore:
    """In-memory chat store shared by chat.human and chat.assistant servers.

    - Monotonic integer sequence for ordering; exposed as string ids
    - Per-server last-read high-water mark (get+advance via read_pending_messages)
    - Broadcasts head updates on the other participant's server when a message arrives
    """

    def __init__(self) -> None:
        self._seq: int = 0
        self._messages: list[tuple[int, ChatMessage]] = []
        # Per-server last-read sequence (None when nothing read yet)
        self._last_read: dict[str, int | None] = {CHAT_HUMAN_SERVER_NAME: None, CHAT_ASSISTANT_SERVER_NAME: None}
        self._servers = _ServerRefs()

    def register_servers(self, *, human: NotifyingFastMCP | None, assistant: NotifyingFastMCP | None) -> None:
        self._servers = _ServerRefs(human=human, assistant=assistant)

    @property
    def last_id(self) -> str | None:
        return str(self._seq) if self._seq > 0 else None

    async def last_id_async(self) -> str | None:
        return self.last_id

    def get_last_read(self, server_name: str) -> str | None:
        val = self._last_read.get(server_name)
        return str(val) if isinstance(val, int) else None

    async def get_last_read_async(self, server_name: str) -> str | None:
        val = self._last_read.get(server_name)
        return str(val) if isinstance(val, int) else None

    def _now_iso(self) -> str:
        return datetime.now(UTC).isoformat()

    async def _notify_other_head(self, *, author: ChatAuthor) -> None:
        head_uri = "chat://head"
        if author is ChatAuthor.USER and self._servers.assistant is not None:
            await self._servers.assistant.broadcast_resource_updated(head_uri)
        elif author is ChatAuthor.ASSISTANT and self._servers.human is not None:
            await self._servers.human.broadcast_resource_updated(head_uri)

    async def _notify_last_read(self, *, server_name: str) -> None:
        srv = self._servers.human if server_name == CHAT_HUMAN_SERVER_NAME else self._servers.assistant
        if srv is not None:
            await srv.broadcast_resource_updated("chat://last-read")

    async def append(self, *, author: ChatAuthor, mime: str, content: str) -> str:
        self._seq += 1
        seq = self._seq
        msg = ChatMessage(id=str(seq), ts=self._now_iso(), author=author, mime=mime, content=content)
        self._messages.append((seq, msg))

        # Notify the other participant's head resource
        await self._notify_other_head(author=author)
        return msg.id

    def get_message(self, msg_id: str) -> ChatMessage | None:
        try:
            seq = int(msg_id)
        except (TypeError, ValueError):
            return None
        for s, m in self._messages:
            if s == seq:
                return m
        return None

    async def get_message_async(self, msg_id: str) -> ChatMessage | None:
        try:
            seq = int(msg_id)
        except (TypeError, ValueError):
            return None
        for s, m in self._messages:
            if s == seq:
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
        self, *, server_name: str, server_author: ChatAuthor, limit: int | None
    ) -> tuple[list[ChatMessage], str | None]:
        other = ChatAuthor.ASSISTANT if server_author is ChatAuthor.USER else ChatAuthor.USER
        after_seq = self._last_read.get(server_name)
        msgs = self._read_since_seq(other_author=other, after_seq=after_seq, limit=limit)
        if msgs:
            # Advance last-read to last message seq and notify
            last_seq = int(msgs[-1].id)
            self._last_read[server_name] = last_seq
            await self._notify_last_read(server_name=server_name)
        return msgs, self.last_id


class ChatStorePersisted(ChatStore):
    """SQLite-backed ChatStore bound to an agent_id.

    Uses SQLitePersistence directly; tables are created via ensure_schema().
    """

    def __init__(self, *, persistence: SQLitePersistence, agent_id: str) -> None:
        super().__init__()
        self._persistence = persistence
        self._agent = agent_id

    async def last_id_async(self) -> str | None:
        async with (
            self._persistence._open_row() as db,
            db.execute("SELECT MAX(id) AS last_id FROM chat_messages WHERE agent_id = ?", (self._agent,)) as cur,
        ):
            if (row := await cur.fetchone()) and (val := row["last_id"]) is not None:
                return str(val)
            return None

    async def get_last_read_async(self, server_name: str) -> str | None:
        async with (
            self._persistence._open_row() as db,
            db.execute(
                "SELECT last_id FROM chat_last_read WHERE agent_id = ? AND server_name = ?", (self._agent, server_name)
            ) as cur,
        ):
            if (row := await cur.fetchone()) and (val := row["last_id"]) is not None:
                return str(val)
            return None

    async def append(self, *, author: ChatAuthor, mime: str, content: str) -> str:
        ts = datetime.now(UTC).isoformat()
        async with self._persistence._open() as db:
            cur = await db.execute(
                "INSERT INTO chat_messages (agent_id, ts, author, mime, content) VALUES (?, ?, ?, ?, ?)",
                (self._agent, ts, author.value, mime, content),
            )
            await db.commit()
            new_id = str(cur.lastrowid)
        # Notify other participant
        await self._notify_other_head(author=author)
        return new_id

    async def get_message_async(self, msg_id: str) -> ChatMessage | None:
        try:
            seq = int(msg_id)
        except (TypeError, ValueError):
            return None
        async with (
            self._persistence._open_row() as db,
            db.execute(
                "SELECT id, ts, author, mime, content FROM chat_messages WHERE agent_id = ? AND id = ?",
                (self._agent, seq),
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
        self, *, server_name: str, server_author: ChatAuthor, limit: int | None
    ) -> tuple[list[ChatMessage], str | None]:
        other = ChatAuthor.ASSISTANT if server_author is ChatAuthor.USER else ChatAuthor.USER
        cap = limit if isinstance(limit, int) and limit > 0 else None
        async with (
            self._persistence._open_row() as db,
            db.execute(
                "SELECT last_id FROM chat_last_read WHERE agent_id = ? AND server_name = ?", (self._agent, server_name)
            ) as cur,
        ):
            r = await cur.fetchone()
            after_seq = r["last_id"] if r else None

        # Fetch messages after HWM
        async with self._persistence._open_row() as db:
            sql = (
                "SELECT id, ts, author, mime, content FROM chat_messages "
                "WHERE agent_id = ? AND id > ? AND author = ? ORDER BY id ASC"
            )
            params: list[object] = [self._agent, (after_seq or 0), other.value]
            if cap is not None:
                sql += " LIMIT ?"
                params.append(cap)
            msgs: list[ChatMessage] = []
            async with db.execute(sql, tuple(params)) as cur:
                async for r in cur:
                    msgs.append(_row_to_message(r))

        if msgs:
            last_seq = int(msgs[-1].id)
            async with self._persistence._open() as db:
                await db.execute(
                    "INSERT INTO chat_last_read (agent_id, server_name, last_id) VALUES (?, ?, ?) "
                    "ON CONFLICT(agent_id, server_name) DO UPDATE SET last_id=excluded.last_id",
                    (self._agent, server_name, last_seq),
                )
                await db.commit()
            await self._notify_last_read(server_name=server_name)
        # last_id: query MAX(id)
        async with (
            self._persistence._open_row() as db,
            db.execute("SELECT MAX(id) AS last_id FROM chat_messages WHERE agent_id = ?", (self._agent,)) as cur,
        ):
            r = await cur.fetchone()
            global_last = r["last_id"] if r else None
        return msgs, (str(global_last) if global_last is not None else None)


def make_chat_server(*, name: str, author: ChatAuthor, store: ChatStore) -> NotifyingFastMCP:
    """Build a chat server bound to a fixed author and a shared store."""

    m = NotifyingFastMCP(name=name, instructions=None)

    # Head sentinel: last_id only (small)
    @m.resource("chat://head", name="chat.head", mime_type="application/json")
    async def head() -> dict:
        return {"last_id": await store.last_id_async()}

    # Last-read HWM (server-managed)
    @m.resource("chat://last-read", name="chat.last_read", mime_type="application/json")
    async def last_read() -> dict:
        return {"last_id": await store.get_last_read_async(name)}

    # Per-message resource for deep links/hydration
    @m.resource("chat://messages/{id}", name="chat.message", mime_type="application/json")
    async def message(id: str) -> ChatMessage:
        got = await store.get_message_async(id)
        if got is None:
            raise KeyError(id)
        return got

    # Tools: post and read_pending_messages (get+advance)
    @m.flat_model()
    async def post(input: PostInput) -> PostResult:
        new_id = await store.append(author=author, mime=input.mime, content=input.content)
        return PostResult(id=new_id)

    @m.flat_model()
    async def read_pending_messages(input: ReadPendingInput) -> ReadPendingResult:
        msgs, last_id = await store.read_pending_and_advance(server_name=name, server_author=author, limit=input.limit)
        return ReadPendingResult(messages=msgs, last_id=last_id)

    return m


async def attach_chat_servers(
    comp, *, human_name: str = CHAT_HUMAN_SERVER_NAME, assistant_name: str = CHAT_ASSISTANT_SERVER_NAME
):
    """Attach chat.human and chat.assistant in-proc backed by a shared store.

    Returns (store, human_server, assistant_server).
    """
    store = ChatStore()
    human = make_chat_server(name=human_name, author=ChatAuthor.USER, store=store)
    assistant = make_chat_server(name=assistant_name, author=ChatAuthor.ASSISTANT, store=store)
    # Register servers for cross-broadcasts
    store.register_servers(human=human, assistant=assistant)
    await comp.mount_inproc(human_name, human)
    await comp.mount_inproc(assistant_name, assistant)
    return store, human, assistant


async def attach_persisted_chat_servers(
    comp,
    *,
    persistence: SQLitePersistence,
    agent_id: str,
    human_name: str = CHAT_HUMAN_SERVER_NAME,
    assistant_name: str = CHAT_ASSISTANT_SERVER_NAME,
):
    store = ChatStorePersisted(persistence=persistence, agent_id=agent_id)
    human = make_chat_server(name=human_name, author=ChatAuthor.USER, store=store)
    assistant = make_chat_server(name=assistant_name, author=ChatAuthor.ASSISTANT, store=store)
    store.register_servers(human=human, assistant=assistant)
    await comp.mount_inproc(human_name, human)
    await comp.mount_inproc(assistant_name, assistant)
    return store, human, assistant
