"""Normalization and validation for globally reserved Agent display names."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

MAX_AGENT_NAME_CODE_POINTS = 80


class InvalidAgentNameError(ValueError):
    """Base class for a display name that cannot be reserved."""


class EmptyAgentNameError(InvalidAgentNameError):
    """Raised when normalization leaves no visible name."""


class AgentNameTooLongError(InvalidAgentNameError):
    """Raised when the normalized display name exceeds the scalar-value limit."""

    def __init__(self, actual_length: int) -> None:
        self.actual_length = actual_length
        super().__init__(
            f"Agent name has {actual_length} Unicode scalar values; maximum is {MAX_AGENT_NAME_CODE_POINTS}."
        )


class ForbiddenAgentNameCharacterError(InvalidAgentNameError):
    """Raised for control, formatting, surrogate, private-use, or unassigned characters."""

    def __init__(self, *, character: str, index: int) -> None:
        self.character = character
        self.index = index
        super().__init__(f"Agent name contains forbidden character U+{ord(character):04X} at position {index + 1}.")


@dataclass(frozen=True, slots=True)
class NormalizedAgentName:
    """A presentation string and its global compatibility-caseless reservation key."""

    display_name: str
    reservation_key: str


def normalize_agent_name(raw_name: str) -> NormalizedAgentName:
    """Return the canonical display form and globally unique comparison key.

    Presentation uses NFC and collapses Unicode whitespace to ordinary spaces. The reservation
    key uses NFKC case-folding, so visually equivalent compatibility forms cannot reserve separate
    Agents. Invisible formatting/control characters are rejected instead of silently discarded.
    """
    display_nfc = unicodedata.normalize("NFC", raw_name)
    for index, character in enumerate(display_nfc):
        if unicodedata.category(character).startswith("C"):
            raise ForbiddenAgentNameCharacterError(character=character, index=index)

    display_name = " ".join(display_nfc.split())
    if not display_name:
        raise EmptyAgentNameError("Agent name must not be empty.")
    if len(display_name) > MAX_AGENT_NAME_CODE_POINTS:
        raise AgentNameTooLongError(len(display_name))

    reservation_key = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", display_name).casefold())
    if not reservation_key:
        raise EmptyAgentNameError("Agent name must not be empty.")
    return NormalizedAgentName(display_name=display_name, reservation_key=reservation_key)
