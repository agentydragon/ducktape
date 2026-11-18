"""Shared markers in BuildBuddy runner logs."""

import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_CARET_ANSI_RE = re.compile(r"\^\[\[[0-9;]*m")
_PROBE_CAS_RE = re.compile(r"CI_VM_PROBE_CAS (?P<rest>.*)")


def strip_ansi(line: str) -> str:
    return _CARET_ANSI_RE.sub("", _ANSI_RE.sub("", line))


def parse_probe_cas(line: str) -> dict[str, str] | None:
    """Read a VM probe archive marker from a line with color codes removed."""
    match = _PROBE_CAS_RE.search(line)
    if match is None:
        return None
    rest = match.group("rest")
    if rest.startswith("digest="):
        digest = rest.removeprefix("digest=")
        return {"digest": digest} if digest else {"missing_digest": "empty"}
    if rest.startswith("missing_digest="):
        return {"missing_digest": rest.removeprefix("missing_digest=")}
    return {"raw": rest}
