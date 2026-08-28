"""The system prompt a chat session is started with.

Prompts belong to Agents: each launchable Agent names its own identity template
(`launchable_agents[].system_prompt_template`). Templates load with a Jinja loader rooted at the
template's own directory, so an identity template pulls the shared attached-chat contract in with
a plain `{% include %}` — the fragment is a fact of the templates, not of the config schema. The
templates are **deploy config, not code and not haku-state**: they are mounted into the console's
ConfigMap. A system prompt is the one instruction surface the agent cannot edit at all,
so the facts whose whole value is that the agent did not choose them belong here; everything an
agent authors for itself stays in its own workspace (see <../../base/README.md>).

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

from jinja2 import BaseLoader, Environment, FileSystemLoader, StrictUndefined


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
    # The durable thread this session serves — rendered so the agent can hand it straight to the
    # conversation-scoped read tools.
    conversation_id: UUID
    workspace: str
    # Oldest first, so the template renders them in reading order.
    recent_messages: Sequence[HistoryMessage]
    # The conversation's earlier sessions, oldest first — rendered so the agent can name them to
    # the conversation-history read tools. Empty for a conversation this session starts.
    earlier_session_ids: Sequence[UUID] = ()


class SystemPromptTemplate:
    """A compiled prompt template, loaded once at startup.

    Loaded at startup rather than per turn: `configMapGenerator` puts a content hash in the
    ConfigMap's name, so editing the template rolls the Deployment anyway, and with
    `maxUnavailable: 0` a broken template is then a pod that never becomes Ready and leaves the
    previous version serving. That only holds if the parse is at construction.
    """

    def __init__(self, source: str, *, loader: BaseLoader | None = None):
        # No autoescaping: the output is markdown handed to a model, never a browser, and
        # HTML-escaping an operator's message body would corrupt what the agent reads.
        environment = Environment(
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
            loader=loader,
        )
        self._template = environment.from_string(source)

    @classmethod
    def from_path(cls, path: Path) -> SystemPromptTemplate:
        """Load one template, resolving `{% include %}` against the template's own directory.

        The include root is the identity template's directory rather than a second configured
        path: the template and the fragments it names ship in one ConfigMap directory, so there
        is no separate root to drift from the files it is supposed to describe.
        """
        return cls(path.read_text(encoding="utf-8"), loader=FileSystemLoader(path.parent))

    def verify_renders(self) -> None:
        """Render representative introductions, so a broken template fails at startup.

        `{% include %}` and `StrictUndefined` both resolve at render, not at parse, and a mistake
        inside a conditional branch only surfaces when that branch executes — so this renders both
        history branches and discards the output. With `configMapGenerator` content-hashing the
        ConfigMap name, a template edit rolls the Deployment, and a pod that cannot render never
        becomes Ready while the previous version keeps serving.
        """
        probe = UUID(int=0)
        self.render(SessionIntroduction(session_id=probe, conversation_id=probe, workspace="/", recent_messages=()))
        self.render(
            SessionIntroduction(
                session_id=probe,
                conversation_id=probe,
                workspace="/",
                recent_messages=(
                    HistoryMessage(sender=HistorySender.OPERATOR, body="probe", sent_at=datetime(2000, 1, 1)),
                ),
                earlier_session_ids=(probe,),
            )
        )

    def render(self, introduction: SessionIntroduction) -> str:
        return self._template.render(
            session_id=introduction.session_id,
            conversation_id=introduction.conversation_id,
            workspace=introduction.workspace,
            recent_messages=list(introduction.recent_messages),
            earlier_session_ids=list(introduction.earlier_session_ids),
        ).strip()
