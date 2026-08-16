"""Everything in a recorded frame that is not the protocol's own vocabulary, removed.

A session exported out of the console's database is production traffic: prose an operator typed,
file contents a `Read` returned, a command line that may carry a token. A fixture is a git object
forever, so the rule here is **fail-closed** — a string survives only because a key names it as
protocol vocabulary, and a key nobody has classified yet elides.

Which of three treatments a value gets is decided by its key, and by nothing about the value:

- **Kept** — the discriminators, tool names and versions the fold and its tests read, plus every
  non-string scalar. `run_in_background: true`, an exit code and a token count are the interesting
  half of these shapes and none of them is prose.
- **Pseudonymised** — identifiers. Eliding them would make five polls of one background shell
  indistinguishable from five polls of five, which is the exact property a monitor-loop fixture
  exists to pin, so each distinct value gets a stable stand-in and equality survives.
- **Elided** — every other string, replaced by a marker carrying its length. This is the default
  branch, so a field the CLI adds tomorrow arrives redacted rather than published.

**The keep-list is by key name, not by path**, so a tool argument that happens to be called `name`
or `model` is kept verbatim. That narrowness is accepted rather than overlooked: the keep-list
holds only names whose values are protocol vocabulary wherever they occur, a kept string past
`_KEPT_LENGTH` elides anyway — a discriminator is never that long and a credential often is — and
an exported fixture is read by a human in the PR that checks it in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Keys whose string value is the protocol's own vocabulary: what a frame is, what it did it with,
# and which release said so. Everything else about a frame is either an identifier below or the
# conversation itself.
_KEPT_KEYS = frozenset(
    {
        "capabilities",
        "claude_code_version",
        "inference_geo",
        "kind",
        "model",
        "name",
        "output_style",
        "permissionMode",
        "role",
        "service_tier",
        "source",
        "state",
        "status",
        "status_category",
        "stop_reason",
        "subagent_type",
        "subtype",
        "task_type",
        "terminal_reason",
        "terminal_slash_commands",
        "tool_name",
        "tools",
        "type",
    }
)

# Keys naming something the projection or a reader groups by. `output_file` is a path rather than
# an identifier, and is here for the same reason: a `task_notification` naming the same file twice
# is a fact, and eliding it deletes it.
_IDENTIFIER_KEYS = frozenset(
    {
        "agent_message_id",
        "bash_id",
        "command_uuid",
        "id",
        "message_id",
        "output_file",
        "parent_tool_use_id",
        "request_id",
        "session_id",
        "shellId",
        "shell_id",
        "summarizes_uuid",
        "task_id",
        "tool_use_id",
        "uuid",
    }
)

# A kept value longer than this elides anyway. No discriminator, tool name or version reaches it;
# a bearer, a PEM body and a base64 blob all pass it comfortably.
_KEPT_LENGTH = 64

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_PREFIXED = re.compile(r"([A-Za-z][A-Za-z0-9]*)_")


@dataclass(slots=True)
class Pseudonyms:
    """Stable stand-ins for one export's identifiers, minted in the order they are first seen.

    Shape-preserving on purpose: a uuid becomes a uuid and a `toolu_…` becomes a `toolu_…`, so a
    fixture still reads as the wire and anything that parses one of these does not start failing on
    the redacted copy.
    """

    _minted: dict[str, str] = field(default_factory=dict)

    def of(self, value: str) -> str:
        if (existing := self._minted.get(value)) is not None:
            return existing
        ordinal = len(self._minted) + 1
        if _UUID.fullmatch(value):
            minted = f"00000000-0000-4000-8000-{ordinal:012d}"
        elif (prefixed := _PREFIXED.match(value)) is not None:
            minted = f"{prefixed.group(1)}_{ordinal}"
        else:
            minted = f"id-{ordinal}"
        self._minted[value] = minted
        return minted


def redact(payload: dict[str, Any], pseudonyms: Pseudonyms) -> dict[str, Any]:
    """One frame with its conversation removed and its structure intact.

    Structure is what survives — every key, every nesting level, every non-string scalar — because
    the projection reads structure and a fixture that lost it would pin nothing.
    """
    return {key: _value(key, value, pseudonyms) for key, value in payload.items()}


def _value(key: str, value: Any, pseudonyms: Pseudonyms) -> Any:
    """What one value under *key* becomes. A list inherits its key, so `tools` keeps tool names."""
    match value:
        # An empty string is a state the projection reads — a text block that said nothing — so it
        # stays one rather than becoming a marker that is itself truthy.
        case "":
            return value
        case str():
            if key in _IDENTIFIER_KEYS:
                return pseudonyms.of(value)
            if key in _KEPT_KEYS and len(value) <= _KEPT_LENGTH:
                return value
            return elided(value)
        case dict():
            return {inner: _value(inner, held, pseudonyms) for inner, held in value.items()}
        case list():
            return [_value(key, item, pseudonyms) for item in value]
        case _:
            return value


def elided(value: str) -> str:
    """What is left of a string: how much of it there was.

    The length is kept because it is the one thing about redacted prose a fixture still needs —
    a message that said something and a message that said nothing are different projections.
    """
    return f"<elided: {len(value)} chars>"
