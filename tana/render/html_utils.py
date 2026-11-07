from __future__ import annotations

import html
import re
from typing import Any

from markdownify import MarkdownConverter

from tana.domain.types import NodeId

# Regex patterns for Tana-specific HTML elements
NODE_SPAN_PATTERN = re.compile(r'<span data-inlineref-node="([^"]+)"></span>')
DATE_SPAN_PATTERN = re.compile(r'<span data-inlineref-date="([^"]+)"></span>')


class _TanaMarkdownConverter(MarkdownConverter):
    """Use Markdownify for HTML conversion while preserving Tana's inline syntax."""

    def process_tag(self, node: Any, parent_tags: set[str] | None = None) -> str:
        converted = str(super().process_tag(node, parent_tags))  # type: ignore[misc]  # Missing from typeshed stubs.
        # Tana's inline formatting convention inserts a space after a comma.
        previous = node.previous_sibling
        if previous is not None and str(previous).endswith(",") and not converted.startswith(" "):
            return " " + converted
        return converted

    def process_text(self, node: Any, parent_tags: set[str] | None = None) -> str:
        # Whitespace and existing TanaPaste syntax are significant in inline names.
        return str(node)

    def convert_i(self, el: Any, text: str, parent_tags: set[str]) -> str:
        return f"_{text.removeprefix(' ')}_"

    convert_em = convert_i

    def convert_u(self, el: Any, text: str, parent_tags: set[str]) -> str:
        return f"__{text.removeprefix(' ')}__"

    def convert_mark(self, el: Any, text: str, parent_tags: set[str]) -> str:
        return f"<mark>{text.removeprefix(' ')}</mark>"

    def convert_strike(self, el: Any, text: str, parent_tags: set[str]) -> str:
        return f"<strike>{text.removeprefix(' ')}</strike>"

    def convert_code(self, el: Any, text: str, parent_tags: set[str]) -> str:
        return f"<code>{text.removeprefix(' ')}</code>"


def html_to_markdown(html_text: str) -> str:
    if "<" not in html_text:
        return html.unescape(html_text)
    return str(
        _TanaMarkdownConverter(
            convert=["b", "strong", "i", "em", "u", "mark", "strike", "code"],
            escape_asterisks=False,
            escape_underscores=False,
        ).convert(html_text)
    )


def find_inline_node_refs(text: str) -> list[NodeId]:
    return [NodeId(match) for match in NODE_SPAN_PATTERN.findall(text)]
