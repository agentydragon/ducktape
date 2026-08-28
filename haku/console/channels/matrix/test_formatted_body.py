"""What survives the trip from Haku's Markdown to what Element will actually render."""

from __future__ import annotations

import pytest_bazel

from haku.console.channels.matrix.formatted_body import to_formatted_body


def test_plain_prose_sends_no_second_copy_of_itself() -> None:
    """`formatted_body` is optional, and a paragraph adds nothing worth a field."""
    assert to_formatted_body("just a sentence") is None


def test_emphasis_and_code_become_html() -> None:
    assert to_formatted_body("**bold** and `code`") == "<p><strong>bold</strong> and <code>code</code></p>"


def test_fenced_code_carries_its_language_as_a_permitted_class() -> None:
    """`language-` is the only class prefix the spec allows on `code`."""
    formatted = to_formatted_body('```python\nprint("hi")\n```')
    assert formatted is not None
    assert '<code class="language-python">' in formatted
    assert formatted.startswith("<pre>")


def test_tables_survive() -> None:
    formatted = to_formatted_body("| a | b |\n| - | - |\n| 1 | 2 |")
    assert formatted is not None
    assert "<table>" in formatted
    assert "<td>1</td>" in formatted


def test_task_lists_keep_their_state_as_text() -> None:
    """`<input type=checkbox>` is not on the allowlist, so a stock renderer loses the box
    entirely and leaves a bare bullet. Haku writes checklists; the state is the content."""
    formatted = to_formatted_body("- [x] shipped\n- [ ] pending")
    assert formatted is not None
    assert "☑ shipped" in formatted
    assert "☐ pending" in formatted
    assert "input" not in formatted


def test_raw_html_the_agent_typed_is_unwrapped_not_forwarded() -> None:
    """Markdown passes raw HTML through, which is exactly why the allowlist is applied to
    the output. The words stay; the tag does not."""
    formatted = to_formatted_body("a <script>alert(1)</script> b")
    assert formatted is not None
    assert "<script>" not in formatted
    assert "alert(1)" in formatted


def test_disallowed_attributes_are_dropped_but_the_tag_stays() -> None:
    formatted = to_formatted_body('<p id="x" style="color:red">text</p>')
    assert formatted is not None
    assert "<p>text</p>" in formatted


def test_an_external_image_becomes_its_alt_text() -> None:
    """`src` must be `mxc://`; a client drops anything else, and fetching it would leak the
    room's readers to whoever serves it."""
    assert to_formatted_body("![a chart](https://example.com/c.png)") == "<p>a chart</p>"


def test_a_link_keeps_a_permitted_scheme_and_loses_others() -> None:
    assert to_formatted_body("[x](https://example.com)") == '<p><a href="https://example.com">x</a></p>'
    formatted = to_formatted_body("[x](javascript:alert(1))")
    assert formatted is not None
    assert "javascript" not in formatted


if __name__ == "__main__":
    pytest_bazel.main()
