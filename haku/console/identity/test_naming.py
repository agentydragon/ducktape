"""Agent display-name normalization and rejection cases."""

from __future__ import annotations

import pytest
import pytest_bazel

from haku.console.identity.naming import (
    MAX_AGENT_NAME_CODE_POINTS,
    AgentNameTooLongError,
    EmptyAgentNameError,
    ForbiddenAgentNameCharacterError,
    normalize_agent_name,
)


@pytest.mark.parametrize("raw_name", ["", " ", "\u00a0\u2003"])
def test_empty_after_unicode_whitespace_normalization_is_rejected(raw_name: str) -> None:
    with pytest.raises(EmptyAgentNameError):
        normalize_agent_name(raw_name)


def test_display_name_is_nfc_with_collapsed_unicode_whitespace() -> None:
    name = normalize_agent_name("  Cafe\u0301\u00a0\u2003  Claude  ")

    assert name.display_name == "Café Claude"
    assert name.reservation_key == "café claude"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Kitchen Claude", "  KITCHEN   CLAUDE "),
        ("Fullwidth", "\uff26\uff55\uff4c\uff4c\uff57\uff49\uff44\uff54\uff48"),
        ("Straße", "STRASSE"),
    ],
)
def test_compatibility_casefold_key_collides_equivalent_names(left: str, right: str) -> None:
    assert normalize_agent_name(left).reservation_key == normalize_agent_name(right).reservation_key


@pytest.mark.parametrize("character", ["\n", "\x00", "\u200d", "\u202e", "\ud800", "\ue000"])
def test_invisible_or_non_public_characters_are_rejected(character: str) -> None:
    with pytest.raises(ForbiddenAgentNameCharacterError):
        normalize_agent_name(f"Claude{character}Agent")


def test_length_limit_counts_normalized_unicode_scalars() -> None:
    accepted = normalize_agent_name("🤖" * MAX_AGENT_NAME_CODE_POINTS)
    assert len(accepted.display_name) == MAX_AGENT_NAME_CODE_POINTS

    with pytest.raises(AgentNameTooLongError) as too_long:
        normalize_agent_name("🤖" * (MAX_AGENT_NAME_CODE_POINTS + 1))
    assert too_long.value.actual_length == MAX_AGENT_NAME_CODE_POINTS + 1


if __name__ == "__main__":
    pytest_bazel.main()
