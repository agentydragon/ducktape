"""haku-console's in-process `haku_index` MCP server — semantic recall over notes and past chats.

Two corpora, two search tools, and for each one a read tool that resolves what search hands back
(`haku/state_index/README.md`). Search is deliberately not a content API: it returns a snippet and
a **pointer**, and the caller reads the real thing.

- **notes** — the files at haku-state's indexed tip. A hit carries the path *and* the blob sha, so
  a caller with a clone reads the exact bytes (`git cat-file blob <sha>`) rather than whatever that
  path holds now. `read_note` is the fallback for a caller with no clone.
- **conversations** — the console's own `claude_chat_messages`. A hit carries the session, the
  Matrix room it served, and the ids of the messages that window holds; `read_messages` returns
  them in full. For the tool calls *underneath* an exchange, `haku_conversations.read_rollout`
  pages the protocol frames instead.

**Reads are unscoped**: any session, whichever room or operator it served — the same open decision
as `conversations.py` (R5.3a). Ranked retrieval is a sharper edge on it than a drilldown, because
it surfaces another room's conversation without anyone naming it. See
`haku/state_index/README.md` § Read scoping before widening who holds these tools.

**Empty is not the same as absent.** An index that has fallen behind returns nothing for a topic
that was discussed at length, so `index_status` exists and the search tools say to check it before
reporting that something was never said.
"""

from __future__ import annotations

import datetime
from typing import Annotated, Protocol
from uuid import UUID

from fastmcp import FastMCP
from pydantic import BaseModel, Field

HAKU_INDEX_SERVER_ID = "haku_index"

# Hits per page. A hit carries a whole chunk of prose, so this is bounded for the same reason
# `haku_conversations` bounds its pages: context is the scarce resource, not rows.
MAX_RESULTS = 25
DEFAULT_RESULTS = 8

# Messages one `read_messages` call will return in full.
MAX_MESSAGES = 50


class NoteHit(BaseModel):
    path: str = Field(description="Path at the indexed commit.")
    blob_sha: str = Field(
        description="Git blob sha of the file this span came from. Read the exact bytes with "
        "`git cat-file blob <sha>`; the path may hold something else by now."
    )
    byte_start: int = Field(description="Start of the matching span, in bytes into the blob.")
    byte_end: int
    snippet: str = Field(description="The text that was matched against — an excerpt, not the file.")
    score: float


class NoteSearchResult(BaseModel):
    commit_sha: str = Field(description="Every path and blob below is at this haku-state commit.")
    branch: str
    indexed_at: datetime.datetime = Field(description="When that commit was indexed, not when it was made.")
    hits: list[NoteHit]


class ConversationHit(BaseModel):
    session_id: str = Field(description="Pass to `read_messages`, or to `haku_conversations` for the frames.")
    room_id: str | None = Field(description="The Matrix room this session served, if it served one.")
    message_ids: list[str] = Field(description="The messages this window holds, in order. Pass to `read_messages`.")
    first_message_at: datetime.datetime
    last_message_at: datetime.datetime
    snippet: str = Field(description="The text that was matched against; `read_messages` returns it untruncated.")
    score: float


class ChatMessage(BaseModel):
    message_id: str
    session_id: str
    role: str = Field(description="`user` or `assistant`.")
    content: str
    created_at: datetime.datetime


class NotesStatus(BaseModel):
    """What the notes corpus holds.

    No backlog figure, and that absence is the honest answer rather than an omission: how far
    behind this corpus is depends on haku-state's current tip, which the console cannot see
    without fetching the repository. `indexed_at` ageing is the signal that its sync has stopped.
    """

    commit_sha: str
    branch: str
    indexed_at: datetime.datetime = Field(description="When that commit was indexed, not when it was made.")
    files: int = Field(description="Paths at the indexed commit, including ones never chunked (binaries, dumps).")
    chunks: int = Field(description="Chunks a search can reach — those still at the tip, under the live regime.")
    superseded_chunks: int = Field(
        description="Chunks embedded under an older chunker or model, which no search can reach. "
        "Nonzero means a re-embed is outstanding."
    )


class ConversationsStatus(BaseModel):
    """What the conversations corpus holds, and how far behind the console's own tables it is.

    The backlog is derived from the same comparison the sync uses to decide what to skip, so it
    cannot disagree with what a sync would actually do.
    """

    sessions: int
    chunks: int
    stale_sessions: int = Field(description="Sessions whose messages have moved on since they were indexed.")
    unindexed_messages: int = Field(description="Completed messages waiting to be embedded. 0 means current.")
    lag_seconds: float | None = Field(
        description="Age of the newest message not yet indexed. Absent when nothing is waiting."
    )
    last_indexed_at: datetime.datetime | None = Field(description="Absent before the first sync.")
    superseded_chunks: int


class IndexStatus(BaseModel):
    notes: NotesStatus | None = Field(description="Absent before the notes corpus has ever been synced.")
    conversations: ConversationsStatus


class IndexSearcher(Protocol):
    """The index, as this server needs it.

    A port rather than an import: the tools are a presentation of the index, and building one
    needs a database engine and an embedder that a schema test has no business constructing.
    """

    async def search_notes(self, query: str, *, limit: int, path_prefix: str | None) -> NoteSearchResult | None: ...

    async def search_conversations(
        self, query: str, *, limit: int, session_id: UUID | None
    ) -> list[ConversationHit]: ...

    async def read_note(self, path: str) -> str | None: ...

    async def read_messages(self, message_ids: list[UUID]) -> list[ChatMessage]: ...

    async def status(self) -> IndexStatus: ...


def build_mcp(searcher: IndexSearcher) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=HAKU_INDEX_SERVER_ID,
        instructions="Semantic recall over haku-state's notes and Haku's own past conversations. "
        "Search returns snippets and pointers; `read_note` and `read_messages` resolve them to full "
        "content. If a search comes back empty, check `index_status` before concluding the subject "
        "never came up — an index that is behind looks exactly like a topic that was never discussed.",
    )

    @mcp.tool
    async def search_notes(
        query: Annotated[str, Field(description="Natural language. This is semantic search, not grep.")],
        limit: Annotated[int, Field(default=DEFAULT_RESULTS, ge=1, le=MAX_RESULTS)] = DEFAULT_RESULTS,
        path_prefix: Annotated[
            str | None, Field(default=None, description="Restrict to paths under this prefix.")
        ] = None,
    ) -> NoteSearchResult:
        """Search haku-state's notes at the indexed commit.

        Prefer this over guessing where something is written down. Each hit carries the blob sha,
        so the file can be read back exactly as it was indexed.
        """
        result = await searcher.search_notes(query, limit=limit, path_prefix=path_prefix)
        if result is None:
            raise ValueError("the notes index is empty — nothing has been synced yet; see `index_status`")
        return result

    @mcp.tool
    async def read_note(path: Annotated[str, Field(description="A path from a `search_notes` hit.")]) -> str:
        """Read an indexed note's text, for a caller with no haku-state clone.

        Not byte-exact: runs of blank lines between chunks are absent, because whitespace-only
        spans are never indexed. It is what search matched against. Anyone holding a clone should
        read the blob sha instead.
        """
        text = await searcher.read_note(path)
        if text is None:
            raise ValueError(f"{path} is not in the index at the indexed commit")
        return text

    @mcp.tool
    async def search_conversations(
        query: Annotated[str, Field(description="Natural language. This is semantic search, not grep.")],
        limit: Annotated[int, Field(default=DEFAULT_RESULTS, ge=1, le=MAX_RESULTS)] = DEFAULT_RESULTS,
        session_id: Annotated[str | None, Field(default=None, description="Restrict to one session.")] = None,
    ) -> list[ConversationHit]:
        """Search what was actually said in past Claude chat sessions, Matrix and SPA alike.

        Use this before answering from memory about prior work, decisions, or things the operator
        asked for earlier: those live here, not in the current context. Only completed messages are
        indexed, so the exchange in flight right now is not searchable.
        """
        return await searcher.search_conversations(
            query, limit=limit, session_id=UUID(session_id) if session_id else None
        )

    @mcp.tool
    async def read_messages(
        message_ids: Annotated[
            list[str], Field(description="Ids from a `search_conversations` hit.", max_length=MAX_MESSAGES)
        ],
    ) -> list[ChatMessage]:
        """Read the full text of messages a search hit named, in conversation order.

        This is the transcript — what was said. For the tool calls an exchange made and the results
        they returned, page the protocol frames with `haku_conversations.read_rollout` instead.
        """
        return await searcher.read_messages([UUID(message_id) for message_id in message_ids])

    @mcp.tool
    async def index_status() -> IndexStatus:
        """How current each corpus is: what it holds, how far behind its source, and how stale.

        Read this when a search returns less than expected. `behind` above zero means content
        exists that the index has not embedded yet, so an empty result is not evidence of absence.
        """
        return await searcher.status()

    return mcp
