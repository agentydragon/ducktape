"""Scrub a probe capture until it is safe to commit, then prove it structurally.

A raw capture is a session transcript taken on somebody's machine: it carries their home directory,
their skill and MCP-server catalog, a socket path with a live PID in it, and — easiest to miss — the
opaque `signature` on every `thinking` block. Keyword-grepping for the obvious finds none of those,
so the check here is **structural**: after scrubbing, no long opaque token and no absolute path
outside `/workspace` may remain, whatever they were called.

    python3 -m haku.cli_protocol.probes.redact_capture raw.jsonl redacted.jsonl

The needles are derived from the running machine (its user, host and home) rather than written down,
so nothing identifying has to be committed to make the check work.
"""

from __future__ import annotations

import getpass
import json
import re
import socket
import sys
from pathlib import Path
from typing import Any

ELIDED = "<elided: {what}>"

# Operator-specific catalogs. Each is a list of what this machine happens to have installed, and
# together they identify the operator more precisely than a name would.
ELIDED_KEYS = {
    "agents": "agent catalog",
    "apiKeySource": "credential source",
    "commands": "command catalog",
    # Not identifying — bulk. `get_context_usage` renders its own `categories` into a grid of ten
    # squares per row, ~13 KB per response and nothing a reader of the protocol needs.
    "gridRows": "context-usage grid, derived from categories",
    "mcp_servers": "MCP server catalog",
    "messaging_socket_path": "PID-bearing socket path",
    "plugins": "plugin catalog",
    "signature": "thinking signature",
    "signature_delta": "thinking signature",
    "skills": "skill catalog",
    "slash_commands": "command catalog",
}

UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
ABSOLUTE_PATH = re.compile(r"/(?:root|home|tmp|Users)(?:/[\w.@%+-]+)*")

# The probe's filler is a closed vocabulary of drill words, so a long run of them is recognisable by
# shape. It carries nothing and is most of the capture's bytes.
FILLER_WORDS = frozenset(
    ("alpha", "bravo", "delta", "echo", "gamma", "kilo", "lima", "mike", "oscar", "tango", "victor", "zulu")
)
FILLER_MIN_WORDS = 50

OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{60,}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
CREDENTIAL = re.compile(r"sk-[A-Za-z0-9-]|Bearer\s|ANTHROPIC_API_KEY")


class Renumberer:
    """Deterministic UUID replacement, numbered by first appearance.

    Position in the log is the only thing a reader may rely on, and it is the only thing this
    preserves: two frames that shared a UUID still share one, and no real UUID survives.
    """

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def __call__(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            value = match.group().lower()
            if value not in self._seen:
                self._seen[value] = f"00000000-0000-4000-8000-{len(self._seen):012d}"
            return self._seen[value]

        return UUID.sub(replace, text)

    def __len__(self) -> int:
        return len(self._seen)


def elide_filler(text: str) -> str:
    words = text.split()
    if len(words) < FILLER_MIN_WORDS or not FILLER_WORDS.issuperset(words):
        return text
    return ELIDED.format(what=f"{len(words)} filler words")


def scrub(value: Any, renumber: Renumberer) -> Any:
    match value:
        case dict():
            return {
                key: ELIDED.format(what=what) if (what := ELIDED_KEYS.get(key)) else scrub(item, renumber)
                for key, item in value.items()
            }
        case list():
            return [scrub(item, renumber) for item in value]
        case str():
            return ABSOLUTE_PATH.sub("/workspace", elide_filler(renumber(value)))
        case _:
            return value


def violations(text: str) -> dict[str, list[str]]:
    """Everything the scrub was supposed to remove, found by shape rather than by name.

    Machine needles match on word boundaries. A short hostname (`vm`) is a substring of ordinary
    words and of the CLI's own request ids, and a check that cries wolf gets turned off.
    """
    machine = {getpass.getuser(), socket.gethostname(), Path.home().name, Path.home().as_posix()}
    return {
        name: found
        for name, found in {
            "opaque tokens": OPAQUE_TOKEN.findall(text),
            "absolute paths": ABSOLUTE_PATH.findall(text),
            "emails": EMAIL.findall(text),
            "credentials": CREDENTIAL.findall(text),
            "machine identity": [
                needle for needle in machine if needle and re.search(rf"\b{re.escape(needle)}\b", text)
            ],
        }.items()
        if found
    }


def main() -> None:
    raw, redacted = Path(sys.argv[1]), Path(sys.argv[2])
    renumber = Renumberer()
    lines = [
        json.dumps(scrub(json.loads(line), renumber), sort_keys=True)
        for line in raw.read_text(encoding="utf-8").splitlines()
        if line
    ]
    redacted.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"{len(lines)} records, {len(renumber)} uuids renumbered, {raw.stat().st_size} -> {redacted.stat().st_size} bytes"
    )

    if found := violations(redacted.read_text(encoding="utf-8")):
        for name, hits in found.items():
            print(f"  UNSAFE {name}: {len(hits)} — e.g. {hits[:3]}")
        raise SystemExit(f"{redacted} is not safe to commit")
    print(f"  clean: no opaque token, absolute path, email, credential or machine identity in {redacted}")


if __name__ == "__main__":
    main()
