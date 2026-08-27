"""The system prompt a Claude chat session is started with.

The template is **deploy config, not code and not haku-state**: it is mounted into the console's
ConfigMap and named by an absolute path in `chat_runtimes.claude_code.system_prompt_template`. A system prompt
is the one instruction surface the agent cannot edit at all, so the facts whose whole value is that
Haku did not choose them belong here; everything Haku authors for itself stays in haku-state (see
<../../base/README.md>).

Rendering is Jinja2 rather than `str.format` because the interesting parts are conditional: a fresh
conversation has no recent messages, and "here is where the conversation was" has to disappear rather than
render as an empty heading.

`StrictUndefined`: a name the template asks for and the renderer does not supply is a deploy-time
mistake, and a system prompt that silently lost a paragraph is not noticed until the agent behaves
oddly a week later.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from jinja2 import Environment, StrictUndefined

from haku.console.mcp_guidance import SERVER_INSTRUCTIONS


class HistorySender(StrEnum):
    OPERATOR = "operator"
    # Provenance only: the message came from the harness/console side of the recorded
    # conversation. It does NOT mean the message went out under the provider LLM API's
    # `assistant` role — rendered history rides inside the system prompt.
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class HistoryMessage:
    """One thing already said in this conversation, either side of it, as we recorded it.

    No channel address: this comes from the console's conversation record, so a replacement can
    resume the thread whichever attached chat surface displayed it. The role is neutral too; the
    template chooses human-readable speaker names.
    """

    sender: HistorySender
    body: str
    sent_at: datetime


@dataclass(frozen=True)
class SessionIntroduction:
    """Everything a rendered prompt may name about the session being started."""

    session_id: UUID
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
            workspace=introduction.workspace,
            recent_messages=list(introduction.recent_messages),
            haku_console_mcp_guidance=SERVER_INSTRUCTIONS,
        ).strip()
