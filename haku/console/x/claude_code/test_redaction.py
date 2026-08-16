"""What survives redaction, and — the half that matters — what does not.

A fixture is a git object forever, so the property under test is the fail-closed one: a key nobody
classified elides, and the three treatments are decided by the key rather than by anything about
the value.
"""

from __future__ import annotations

import pytest_bazel

from haku.console.x.claude_code.redaction import Pseudonyms, redact


def test_a_key_nobody_classified_elides():
    """The default branch, which is what makes a field the next CLI release adds safe by default."""
    redacted = redact({"type": "assistant", "a_field_added_next_release": "an operator's own words"}, Pseudonyms())

    assert redacted == {"type": "assistant", "a_field_added_next_release": "<elided: 23 chars>"}


def test_prose_elides_down_to_its_length():
    """Length is kept because the projection reads it: a message that said something and one that
    said nothing are different events."""
    frame = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "the answer"}, {"type": "thinking", "signature": "EqQBCkYIBxgCK"}],
        },
    }

    assert redact(frame, Pseudonyms()) == {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "<elided: 10 chars>"},
                {"type": "thinking", "signature": "<elided: 13 chars>"},
            ],
        },
    }


def test_an_empty_string_stays_empty():
    """A marker would be truthy, and an empty text block is a state the fold distinguishes."""
    assert redact({"type": "result", "result": ""}, Pseudonyms()) == {"type": "result", "result": ""}


def test_a_tool_call_keeps_its_name_and_its_flags_and_loses_its_command():
    """The shape a background-command fixture exists for: `run_in_background` is the finding and
    the command line is the thing that might carry a token."""
    block = {
        "type": "tool_use",
        "id": "toolu_01ABCDEF",
        "name": "Bash",
        "input": {"command": "curl -H 'Authorization: Bearer sk-real'", "run_in_background": True, "timeout": 120},
    }

    assert redact(block, Pseudonyms()) == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "Bash",
        "input": {"command": "<elided: 39 chars>", "run_in_background": True, "timeout": 120},
    }


def test_identifiers_are_pseudonymised_so_sameness_and_difference_both_survive():
    """Five polls of one background shell must stay distinguishable from five polls of five."""
    pseudonyms = Pseudonyms()

    first = redact({"type": "tool_use", "name": "BashOutput", "input": {"bash_id": "bash_7"}}, pseudonyms)
    again = redact({"type": "tool_use", "name": "BashOutput", "input": {"bash_id": "bash_7"}}, pseudonyms)
    other = redact({"type": "tool_use", "name": "BashOutput", "input": {"bash_id": "bash_9"}}, pseudonyms)

    assert first == again
    assert first != other


def test_a_pseudonym_keeps_the_shape_of_what_it_replaces():
    """So a fixture still reads as the wire, and anything that parses one of these keeps parsing."""
    redacted = redact(
        {"type": "assistant", "uuid": "6f3c9b18-1d6a-4a51-9d0e-9d3b1f0a2c44", "message": {"id": "msg_01XYZ"}},
        Pseudonyms(),
    )

    assert redacted == {"type": "assistant", "uuid": "00000000-0000-4000-8000-000000000001", "message": {"id": "msg_2"}}


def test_a_list_of_strings_inherits_its_key():
    """`tools` names the CLI's own catalog; `skills` and the rest are the operator's."""
    frame = {"type": "system", "subtype": "init", "tools": ["Bash", "Read"], "skills": ["cpap", "freecad"]}

    assert redact(frame, Pseudonyms()) == {
        "type": "system",
        "subtype": "init",
        "tools": ["Bash", "Read"],
        "skills": ["<elided: 4 chars>", "<elided: 7 chars>"],
    }


def test_a_kept_key_carrying_something_long_elides_anyway():
    """The one rule that reads the value: no discriminator is 64 characters and a bearer is."""
    smuggled = "sk-ant-" + "x" * 80

    assert redact({"type": "system", "model": smuggled}, Pseudonyms()) == {
        "type": "system",
        "model": f"<elided: {len(smuggled)} chars>",
    }


def test_numbers_and_booleans_and_nulls_are_kept_whatever_their_key():
    """The interesting half of a tool result: an exit code, a duration, a flag."""
    structured = {
        "tool_use_result": {"exit_code": 1, "interrupted": False, "stdout": "boom", "returnCodeInterpretation": None}
    }

    assert redact(structured, Pseudonyms()) == {
        "tool_use_result": {
            "exit_code": 1,
            "interrupted": False,
            "stdout": "<elided: 4 chars>",
            "returnCodeInterpretation": None,
        }
    }


if __name__ == "__main__":
    pytest_bazel.main()
