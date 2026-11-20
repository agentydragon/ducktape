from __future__ import annotations

import re
from html.parser import HTMLParser
from io import StringIO

from tana.domain.types import NodeId

# Regex patterns for Tana-specific HTML elements
NODE_SPAN_PATTERN = re.compile(r'<span data-inlineref-node="([^"]+)"></span>')
DATE_SPAN_PATTERN = re.compile(r'<span data-inlineref-date="([^"]+)"></span>')


# (opening, closing) delimiter per formatting tag; `mark`, `strike` and `code` stay HTML.
_DELIMITERS = {
    "b": ("**", "**"),
    "strong": ("**", "**"),
    "i": ("_", "_"),
    "em": ("_", "_"),
    "u": ("__", "__"),
    "mark": ("<mark>", "</mark>"),
    "strike": ("<strike>", "</strike>"),
    "code": ("<code>", "</code>"),
}


class HTMLToMarkdownParser(HTMLParser):
    """Convert HTML formatting to Markdown syntax."""

    def __init__(self):
        super().__init__()
        self.output = StringIO()

    def handle_starttag(self, tag, attrs):
        prev = self.output.getvalue()
        # If a formatting tag follows a comma without a space, insert one (",**" -> ", **")
        if prev.endswith(",") and not prev.endswith(", "):
            # Only add a space if not already present
            self.output.write(" ")
        if delimiters := _DELIMITERS.get(tag):
            self.output.write(delimiters[0])

    def handle_endtag(self, tag):
        if delimiters := _DELIMITERS.get(tag):
            self.output.write(delimiters[1])

    def handle_data(self, data):
        # If a data chunk begins with a space immediately after opening a formatting marker,
        # drop that single space to avoid sequences like "** _italic_**".
        if data.startswith(" ") and self.output.getvalue().endswith(("**", "_", "__", "<mark>", "<strike>", "<code>")):
            data = data[1:]
        self.output.write(data)


def html_to_markdown(html_text: str) -> str:
    parser = HTMLToMarkdownParser()
    parser.feed(html_text)
    return parser.output.getvalue()


def find_inline_node_refs(text: str) -> list[NodeId]:
    return [NodeId(match) for match in NODE_SPAN_PATTERN.findall(text)]
