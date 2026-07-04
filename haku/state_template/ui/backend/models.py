"""Typed models the backend reads from haku-state and serves over JSON.

Each content collection carries thin, typed frontmatter (``items/<slug>.md``,
``memory/improvements/<id>.md``, ``runs/<date>/<ulid>.md``); the validate-state gate
parses every file through these models so a malformed frontmatter file can't silently
parse into a wrong shape. Typed values (enum, dates) validate strictly.

Keep the frontend's ``types.ts`` in sync by hand.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field, Tag


class ItemStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    REJECTED = "rejected"
    SNOOZED = "snoozed"
    EXPIRED = "expired"


# --- Item (items/<slug>.md) ----------------------------------------------------
# An item is typed frontmatter + a markdown body that embeds affordances (<handoff> for the
# executor prompt, <signal-toggle> for the operator's status). Only fields with a real reader
# survive — Inbox display/ranking/filter/timing. This model gates the FRONTMATTER; the body is
# prose validated only structurally, like other content-directory docs. The readable filename is
# the item's identity (no `id` field). See items/README.md.


class ItemDoc(BaseModel):
    title: str
    value: int = Field(ge=0, le=100)  # Inbox ranking
    status: ItemStatus  # Inbox filter (open shown)
    deadline: datetime | None = None  # optional — due-soon badge/sort
    snoozed_until: date | None = None  # optional — resurface timing


# --- API request/response models (the JSON contract for the React SPA) ---------


class MetaResponse(BaseModel):
    """Footer metadata: when haku-state last changed, and which image is serving. Items are read
    from `items/*.md` through the generic proxy, not here."""

    scan_time: str = Field(description="ISO 8601 timestamp of the last haku-state scan (newest Haku commit)")
    deployed_commit: str | None = Field(default=None, description="Short SHA the running UI image was built from")
    deployed_commit_url: str | None = Field(default=None, description="Forgejo link to the deployed commit")


class FeedbackRequest(BaseModel):
    text: str
    item_id: str | None = Field(
        default=None,
        description="If set, the item this feedback is about (tagged in the intake note); else a global note",
    )
    page: str | None = Field(
        default=None, description="The UI page (URL hash, e.g. '#/runs') the operator was on when writing the note"
    )
    selection: str | None = Field(
        default=None,
        description="Any text the operator had selected on the page, for grounding (e.g. 'this looks bad')",
    )


# --- Responses surface (responses/<scope>/<field>.yaml) ------------------------
# The generic operator-answer log: one keyed current-state file per (scope, field) slot, committed
# per change so the git commit history IS the append-only log (plans/ui-authoring-architecture.md →
# feedback loop). `scope` is an item id / form id / context key, `field` is the slot; the file at
# HEAD is the current answer. The item status slot and forms compose over this. Writes stay a
# dedicated endpoint; reads go through the generic proxy.


class ResponseRequest(BaseModel):
    value: str
    note: str | None = None


class ResponseDoc(BaseModel):
    # scope/field are the path, not fields in the file — nothing else describes them, so no need.
    value: str
    note: str | None = None
    at: str | None = None


# --- Improvements surface (memory/improvements/<id>.md content collection) -----
# Haku's self-backlog is a content collection: one markdown file per entry, flat
# `kind: improvement` frontmatter + detail prose as the body, rendered live by the
# <improvement-board/> garden widget over the tree+blobs proxy. This model is the
# frontmatter schema the validate-state gate checks; the widget parses defensively.


class ImprovementDoc(BaseModel):
    kind: Literal["improvement"]
    doc_class: Literal["idea", "friction"] = Field(alias="class")  # which board section
    title: str
    weight: Literal["high", "medium", "low"]  # idea → value, friction → severity
    status: str  # idea: recommend|idea|parked|blocked|wired; friction: open|workaround|resolved|answered
    summary: str = ""  # ideas carry a one-liner; friction usually omits it


# --- Runs surface (runs/<date>/<ulid>.md) --------------------------------------
# Per-run propagation record: proves every source was processed and shows how each change
# propagated to every surface. One markdown file per run — this manifest as YAML frontmatter (the
# machine-checkable spine, validated below), prose reasoning as the body (rendered in the Runs
# tab). See procedures/propagation/ + the base "Propagation discipline" obligation.


class ScannedSource(BaseModel):
    """A source read this run: where its bookmark moved and how many changes it yielded."""

    source: str
    # Bookmarks are opaque resume tokens and differ by source (an email epoch string, a REST API's
    # int id, a millisecond timestamp), so accept either an int or a string.
    bookmark_before: int | str | None = None
    bookmark_after: int | str | None = None
    # A real count when countable (0 = scanned, nothing new); a short prose summary when a
    # count would be misleading (e.g. "2 commits, neither touches base"). Never absence.
    changes_seen: int | str = 0


class SkippedSource(BaseModel):
    """A source NOT read this run, with the reason — so a coverage gap is loud, not silent."""

    source: str
    skipped: str


def _source_kind(value: Any) -> str:
    """Discriminate a run source: a row carrying a ``skipped`` reason is the skipped variant."""
    skipped = value.get("skipped") if isinstance(value, dict) else getattr(value, "skipped", None)
    return "skipped" if skipped else "scanned"


# Either-or by construction: a source was scanned (bookmarks + count) XOR skipped (reason). The
# manifest YAML stays lean (no explicit `kind` tag) — the variant is inferred from `skipped`.
RunSource = Annotated[
    Annotated[ScannedSource, Tag("scanned")] | Annotated[SkippedSource, Tag("skipped")], Discriminator(_source_kind)
]


class RunChecklist(BaseModel):
    checklist: str  # filename stem under procedures/propagation/
    ref: str = ""
    walked: bool = False


class PropagationTarget(BaseModel):
    surface: str
    # "created" = a new entry/file was made on the surface; "n/a" = this surface never
    # applies to this change; "no_change" = considered, didn't apply.
    action: Literal["created", "updated", "no_change", "n/a"]
    note: str = ""


class PropagationEntry(BaseModel):
    change: str
    source: str = ""  # which source the change came from
    surfaces: list[PropagationTarget] = Field(default_factory=list)


class RunManifest(BaseModel):
    run_id: str
    date: str = ""
    started: str = ""
    finished: str = ""
    sources: list[RunSource] = Field(default_factory=list)
    checklists: list[RunChecklist] = Field(default_factory=list)
    propagation: list[PropagationEntry] = Field(default_factory=list)
    # Prose notes are the markdown *body* of the run's .md, not a manifest field: the frontend
    # reads them from the body; validate-state checks only this frontmatter.


class RunsResponse(BaseModel):
    runs: list[RunManifest] = Field(default_factory=list)


# The knowledge garden browses/reads arbitrary repo markdown through the generic content
# proxy below (the frontend filters the tree to the curated dirs and fetches blobs) — no
# dedicated garden model or endpoint.

# --- Generic content proxy: Forgejo's two read primitives, thinly passed through ------
# `/api/repo/tree` mirrors Forgejo's recursive git-trees API; `/api/repo/blobs` mirrors its
# bulk blob fetch. The frontend composes (filter the tree by prefix/kind, then fetch the blobs
# it wants), so new collections/views — and migrating existing server-side reads — need zero
# backend shape changes. See plans/garden-gradient.md → Settled mechanism.


class RepoTreeEntry(BaseModel):
    path: str  # repo-relative
    type: str  # git object type, straight from Forgejo: "blob" | "tree"
    sha: str


class RepoTree(BaseModel):
    sha: str  # the HEAD commit the tree was read at (for the client to cache/keying)
    entries: list[RepoTreeEntry] = Field(default_factory=list)


class RepoBlob(BaseModel):
    sha: str
    content: str  # UTF-8 text (haku-state is a text repo)
