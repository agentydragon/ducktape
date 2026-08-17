"""The system prompt a Claude chat session is started with.

The template is **deploy config, not code and not haku-state**: it is mounted into the console's
ConfigMap and named by an absolute path in `claude_runtime.system_prompt_template`. A system prompt
is the one instruction surface the agent cannot edit at all, so the facts whose whole value is that
Haku did not choose them belong here; everything Haku authors for itself stays in haku-state (see
<../../base/README.md>).

Rendering is Jinja2 rather than `str.format` because the interesting parts are conditional: a fresh
room has no recent messages, and "here is where the conversation was" has to disappear rather than
render as an empty heading.

`StrictUndefined`: a name the template asks for and the renderer does not supply is a deploy-time
mistake, and a system prompt that silently lost a paragraph is not noticed until the agent behaves
oddly a week later.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from jinja2 import Environment, StrictUndefined


@dataclass(frozen=True)
class HistoryMessage:
    """One thing already said in this conversation, either side of it, as we recorded it.

    No channel address: this comes from the console's transcript rather than from the room
    (`channels/matrix/sync.py`'s `recent_history`), and what a prompt was folded from is a field on
    its `PROMPT_ENQUEUED` event rather than something the text carries. Nothing renders one into a
    prompt — the agent cannot resolve one until the room read tools land, and until then an
    address in front of the operator's words is noise it is invited to quote back.
    """

    sender: str
    body: str
    sent_at: datetime


@dataclass(frozen=True)
class SessionIntroduction:
    """Everything a rendered prompt may name about the session being started."""

    session_id: UUID
    room_id: str
    operator_user_id: str
    workspace: str
    # Oldest first, so the template renders them in reading order.
    recent_messages: Sequence[HistoryMessage]


class SystemPromptTemplate:
    """A compiled prompt template, loaded once at startup.

    Loaded at startup rather than per turn: `configMapGenerator` puts a content hash in the
    ConfigMap's name, so editing the template rolls the Deployment anyway, and with
    `maxUnavailable: 0` a broken template is then a pod that never becomes Ready and leaves the
    previous version serving. That only holds if the parse is at construction.
    """

    def __init__(self, source: str):
        # No autoescaping: the output is markdown handed to a model, never a browser, and
        # HTML-escaping an operator's message body would corrupt what the agent reads.
        environment = Environment(
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._template = environment.from_string(source)

    @classmethod
    def from_path(cls, path: Path) -> SystemPromptTemplate:
        return cls(path.read_text(encoding="utf-8"))

    def render(self, introduction: SessionIntroduction) -> str:
        return self._template.render(
            session_id=introduction.session_id,
            room_id=introduction.room_id,
            operator_user_id=introduction.operator_user_id,
            workspace=introduction.workspace,
            recent_messages=list(introduction.recent_messages),
        ).strip()
