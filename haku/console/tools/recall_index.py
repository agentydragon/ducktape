"""haku-console's in-process `haku_index` MCP server — semantic recall, and nothing else.

One `search` over two corpora (`haku/recall_index/README.md`), selected by argument rather than
split into two tools: the caller's question is "where was this said or written", and which body of
text answers it is the search's job to work out, not a routing decision to make before asking.

**Search returns pointers, not content.** A hit carries a snippet and enough identity to fetch the
real thing where it already lives — a path and blob sha at a named commit for haku-state, a session and
its message ids for conversations. Deliberately no read tools here: Haku reads haku-state from its
own clone, and `haku_conversations` already owns reading past sessions. A second reader in this
server would be a second answer to "what does this file say", and the two would drift.

**Reads are unscoped**: any session, whichever room or operator it served — the same open decision
as `conversations.py`. Ranked retrieval is a sharper edge on it than a drilldown, because
it surfaces another room's conversation without anyone naming it. See
`haku/recall_index/README.md` § Read scoping before widening who holds this tool.

**Empty is not the same as absent.** An index that has fallen behind returns nothing for a topic
discussed at length, so `index_status` exists and `search` says to check it before reporting that
something was never said.
"""

from __future__ import annotations

import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastmcp import FastMCP
from pydantic import BaseModel, Field

HAKU_INDEX_SERVER_ID = "haku_index"

# Hits per call. A hit carries a whole chunk of prose, so this is bounded for the same reason
# `haku_conversations` bounds its pages: context is the scarce resource, not rows.
MAX_RESULTS = 25
DEFAULT_RESULTS = 8


class SearchCorpus(StrEnum):
    """Which body of text to search.

    `haku_state` is the files at the indexed tip of the haku-state repository — named for the
    repository rather than for what is in it, because that is exactly as much as the wiring
    promises. Storage calls the two `git` and `chat` (`recall_index.schema.Corpus`), and
    `recall_index_reader` is the one place that translation happens.
    """

    HAKU_STATE = "haku_state"
    CONVERSATIONS = "conversations"
    ALL = "all"


class HakuStateSource(BaseModel):
    """Where a hit sits in the haku-state repository, at the commit the index holds."""

    kind: Literal["haku_state"] = "haku_state"
    path: str = Field(description="Path at `commit_sha`.")
    commit_sha: str = Field(description="The haku-state commit this hit is from. Read the file there.")
    blob_sha: str = Field(
        description="Git blob sha of the file this span came from — `git cat-file blob <sha>` in a "
        "haku-state clone gets the exact bytes, whatever the path holds now."
    )
    byte_start: int = Field(description="Start of the matching span, in bytes into the blob.")
    byte_end: int


class ConversationSource(BaseModel):
    """Where a hit sits in a past Claude chat session."""

    kind: Literal["conversation"] = "conversation"
    session_id: UUID = Field(description="Pass to `haku_conversations` to read the session around this.")
    room_id: str | None = Field(description="The Matrix room this session served, if it served one.")
    message_ids: list[UUID] = Field(description="The messages this window holds, in order.")
    first_message_at: datetime.datetime
    last_message_at: datetime.datetime


class SearchHit(BaseModel):
    """One match: how well it matched, what matched, and where to go read it.

    The corpora differ only in the last of those, so only that is a union — a hit's score and
    snippet mean the same thing whichever body of text produced it, and duplicating them per
    corpus would invite them to drift apart.
    """

    score: float
    snippet: str = Field(description="The text that was matched against — an excerpt, not the source.")
    source: HakuStateSource | ConversationSource = Field(
        discriminator="kind", description="Where this came from, and what it takes to read it there."
    )


class HakuStateStatus(BaseModel):
    """What the haku-state corpus holds, and how far that is from what the repository holds.

    Reported even before anything has been indexed, because "nothing yet" and "never asked" and
    "failing every sweep" are different answers and an absent object gave all three the same
    shape. A first index of a whole repository takes minutes of embedding, and during it
    `indexed_commit` is null while `embedded_chunks` climbs.
    """

    indexed_commit: str | None = Field(
        default=None, description="The commit a search reaches. Null until a first sync has completed."
    )
    remote_commit: str | None = Field(
        default=None,
        description="What the branch pointed at when a sweep last looked. Differing from "
        "`indexed_commit` means a sync is outstanding or in progress; null means no sweep has "
        "reached the repository yet, which is the shape of a misconfigured or unreachable remote.",
    )
    remote_seen_at: datetime.datetime | None = Field(
        default=None, description="When a sweep last reached the remote. Ageing means the sweep has stopped."
    )
    branch: str | None = None
    indexed_at: datetime.datetime | None = Field(
        default=None, description="When `indexed_commit` was indexed, not when it was made."
    )
    files: int = Field(default=0, description="Paths at the indexed commit, including ones never chunked.")
    chunks: int = Field(
        default=0, description="Chunks a search can reach — those still at the tip, under the live regime."
    )
    embedded_chunks: int = Field(
        default=0,
        description="Chunks embedded under the live regime, whether or not the tip reaches them. This is "
        "the one that moves during a first index, since embeddings commit as they are computed while "
        "the tip is swapped only at the end.",
    )
    superseded_chunks: int = Field(
        default=0,
        description="Chunks embedded under an older chunker or model, which no search can reach. "
        "Nonzero means a re-embed is outstanding.",
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
    haku_state: HakuStateStatus = Field(
        description="Always present: before a first index its `indexed_commit` is null and "
        "`remote_commit` says whether a sweep has reached the repository at all."
    )
    conversations: ConversationsStatus


class SearchResults(BaseModel):
    """Hits, plus the index's own state when it is behind enough to explain a thin result."""

    hits: list[SearchHit]
    index: IndexStatus | None = Field(
        default=None,
        description="Present only when a searched corpus is behind its source by more than its "
        "sweep takes. The numbers are the point: how many messages are waiting, how long the lag "
        "is, which commit is indexed against which the branch holds. Absent means the corpora "
        "searched were current, so an empty result is evidence of absence.",
    )


class IndexSearcher(Protocol):
    """The index, as this server needs it.

    A port rather than an import: the tools are a presentation of the index, and building one
    needs a database engine and an embedder that a schema test has no business constructing.
    """

    async def search(
        self, query: str, *, corpus: SearchCorpus, limit: int, path_prefix: str | None, session_id: UUID | None
    ) -> SearchResults: ...

    async def status(self) -> IndexStatus: ...


def build_mcp(searcher: IndexSearcher) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=HAKU_INDEX_SERVER_ID,
        instructions="Semantic recall over haku-state's files and Haku's own past conversations. "
        "`search` returns snippets and pointers — a path and blob sha to read from a haku-state "
        "clone, or a session id to read through `haku_conversations`. If a search comes back "
        "empty, check `index_status` before concluding the subject never came up: an index that "
        "is behind looks exactly like a topic that was never discussed.",
    )

    @mcp.tool
    async def search(
        query: Annotated[str, Field(description="Natural language. This is semantic search, not grep.")],
        corpus: Annotated[
            SearchCorpus, Field(default=SearchCorpus.ALL, description="Which body of text to search. Both, by default.")
        ] = SearchCorpus.ALL,
        limit: Annotated[int, Field(default=DEFAULT_RESULTS, ge=1, le=MAX_RESULTS)] = DEFAULT_RESULTS,
        path_prefix: Annotated[
            str | None, Field(default=None, description="haku-state only: restrict to paths under this prefix.")
        ] = None,
        session_id: Annotated[
            UUID | None, Field(default=None, description="Conversations only: restrict to one session.")
        ] = None,
    ) -> SearchResults:
        """Search haku-state's files and what was said in past sessions.

        **Recall step, not an optional one.** Run this before answering about prior work,
        decisions, dates, people, preferences, commitments, or anything the operator asked for
        earlier: those live here, not in the context in front of you. Results are ranked together
        across corpora, so a note and a conversation compete on how well they match.

        **If it comes back thin, say that you looked.** "I searched and found nothing about that"
        is a different claim from silence, and only one of them is honest. If `index` is present
        the corpus was behind when you asked — quote what it says, because "nothing indexed yet"
        and "nothing was ever said" are not the same answer.

        Each hit is a pointer to content that lives elsewhere: read a file from a haku-state clone
        at the `commit_sha`/`blob_sha` it names, and a conversation through `haku_conversations`.
        Only completed messages are indexed, so the exchange in flight right now is not searchable.
        """
        return await searcher.search(query, corpus=corpus, limit=limit, path_prefix=path_prefix, session_id=session_id)

    @mcp.tool
    async def index_status() -> IndexStatus:
        """How current each corpus is: what it holds, how far behind its source, and how stale.

        Read this when a search returns less than expected. A nonzero `unindexed_messages` means
        content exists that the index has not embedded yet, so an empty result is not evidence of
        absence.
        """
        return await searcher.status()

    return mcp
