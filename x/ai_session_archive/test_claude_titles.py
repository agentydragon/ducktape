from typing import Any

import pytest
import pytest_bazel

from x.ai_session_archive.claude_titles import ClaudeTitle, ClaudeTitleSource, select_claude_title


@pytest.mark.parametrize("content", ["Injected instructions", [{"type": "text", "text": "Injected instructions"}]])
def test_meta_user_records_are_not_prompt_titles(content: Any) -> None:
    assert select_claude_title(None, {"type": "user", "isMeta": True, "message": {"content": content}}) is None


def test_local_command_sequence_leaves_first_ordinary_prompt_as_title() -> None:
    records: list[dict[str, Any]] = [
        {
            "type": "user",
            "isMeta": True,
            "message": {"content": "<local-command-caveat>Local command</local-command-caveat>"},
        },
        {"type": "user", "message": {"content": "<command-name>/status</command-name>"}},
        {"type": "user", "message": {"content": "<local-command-stdout>Ready</local-command-stdout>"}},
        {"type": "user", "message": {"content": "<local-command-stderr>Example error</local-command-stderr>"}},
        {"type": "user", "message": {"content": "Inspect the parser"}},
        {"type": "user", "message": {"content": "A later follow-up"}},
    ]
    title = None
    for record in records:
        title = select_claude_title(title, record)
    assert title == ClaudeTitle(text="Inspect the parser", source=ClaudeTitleSource.PROMPT)


def test_fork_user_record_uses_directive_after_boilerplate() -> None:
    # Captured fork records mix an inherited tool result and the directive's text block.
    record = {
        "type": "user",
        "isSidechain": True,
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "example-tool", "content": "Inherited result"},
                {
                    "type": "text",
                    "text": "<fork-boilerplate>\nYou are a worker fork.\n</fork-boilerplate>\nYour directive: Inspect the parser",
                },
            ]
        },
    }
    assert select_claude_title(None, record) == ClaudeTitle(text="Inspect the parser", source=ClaudeTitleSource.PROMPT)


def test_tool_result_text_is_not_a_prompt_and_ordinary_markup_is_retained() -> None:
    assert (
        select_claude_title(None, {"type": "user", "message": {"content": [{"type": "tool_result", "text": "Result"}]}})
        is None
    )
    assert select_claude_title(
        None, {"type": "user", "message": {"content": "<request>Inspect this markup</request>"}}
    ) == ClaudeTitle(text="<request>Inspect this markup</request>", source=ClaudeTitleSource.PROMPT)


def test_native_titles_override_prompt_fallback_and_custom_renames_win() -> None:
    records: list[dict[str, Any]] = [
        {"type": "user", "message": {"content": "Initial prompt"}},
        {"type": "summary", "summary": "Generated summary", "leafUuid": "example-leaf"},
        {"type": "summary", "summary": "Updated summary", "leafUuid": "example-leaf"},
        {"type": "custom-title", "customTitle": "Custom name", "sessionId": "example-session"},
        {"type": "summary", "summary": "Later summary", "leafUuid": "example-leaf"},
        {"type": "custom-title", "customTitle": "Renamed session", "sessionId": "example-session"},
        {"type": "user", "message": {"content": "A later prompt"}},
    ]
    title = None
    for record in records[:3]:
        title = select_claude_title(title, record)
    assert title == ClaudeTitle(text="Updated summary", source=ClaudeTitleSource.SUMMARY)
    for record in records[3:]:
        title = select_claude_title(title, record)
    assert title == ClaudeTitle(text="Renamed session", source=ClaudeTitleSource.CUSTOM)


if __name__ == "__main__":
    pytest_bazel.main()
