from __future__ import annotations

import html
import re
from collections.abc import Callable
from html.parser import HTMLParser
from io import StringIO

from tana.domain.types import NodeId
from tana.render.inline_refs import parse_inline_date

# Regex patterns for Tana-specific HTML elements
NODE_SPAN_PATTERN = re.compile(r'<span data-inlineref-node="([^"]+)"></span>')
DATE_SPAN_PATTERN = re.compile(r'<span data-inlineref-date="([^"]+)"></span>')


class HTMLToMarkdownParser(HTMLParser):
    """Convert HTML formatting to Markdown syntax."""

    def __init__(self):
        super().__init__()
        self.output = StringIO()
        self.tag_stack = []
        # When starting an <em> immediately after **, suppress a single leading space in next data
        self._suppress_next_leading_space = False

    def handle_starttag(self, tag, attrs):
        self.tag_stack.append(tag)
        prev = self.output.getvalue()
        # If a formatting tag follows a comma without a space, insert one (",**" -> ", **")
        if prev.endswith(",") and not prev.endswith(", "):
            # Only add a space if not already present
            self.output.write(" ")
        if tag in ("b", "strong"):
            self.output.write("**")
        elif tag in ("i", "em"):
            # If italic starts right after bold, drop a single leading space from the next data chunk
            if prev.endswith("**"):
                self._suppress_next_leading_space = True
            self.output.write("_")
        elif tag == "u":
            self.output.write("__")
        elif tag == "mark":
            self.output.write("<mark>")
        elif tag == "strike":
            self.output.write("<strike>")
        elif tag == "code":
            self.output.write("<code>")

    def handle_endtag(self, tag):
        if self.tag_stack and self.tag_stack[-1] == tag:
            self.tag_stack.pop()
        if tag in ("b", "strong"):
            self.output.write("**")
        elif tag in ("i", "em"):
            self.output.write("_")
        elif tag == "u":
            self.output.write("__")
        elif tag == "mark":
            self.output.write("</mark>")
        elif tag == "strike":
            self.output.write("</strike>")
        elif tag == "code":
            self.output.write("</code>")

    def handle_data(self, data):
        # If a data chunk begins with a space immediately after opening a formatting marker,
        # drop that single space to avoid sequences like "** _italic_**".
        if data.startswith(" ") and self.output.getvalue().endswith(("**", "_", "__", "<mark>", "<strike>", "<code>")):
            data = data[1:]
        self._suppress_next_leading_space = False
        self.output.write(data)

    def get_markdown(self) -> str:
        return self.output.getvalue()


def html_to_markdown(html_text: str) -> str:
    parser = HTMLToMarkdownParser()
    parser.feed(html_text)
    return parser.get_markdown()


def process_inline_refs(
    text: str,
    node_formatter: Callable[[str], str] | None = None,
    date_formatter: Callable[[str], str] | None = None,
    unescape: bool = True,
) -> str:
    """node_formatter takes node ID, date_formatter takes ISO date string."""
    # Process node references
    if node_formatter:
        text = NODE_SPAN_PATTERN.sub(lambda m: node_formatter(m.group(1)), text)

    # Process date references
    if date_formatter:

        def date_sub(m):
            iso_date = parse_inline_date(m.group(1))
            return date_formatter(iso_date)

        text = DATE_SPAN_PATTERN.sub(date_sub, text)

    # Unescape HTML entities if requested
    if unescape:
        text = html.unescape(text)

    return text


def find_inline_node_refs(text: str) -> list[NodeId]:
    return [NodeId(match) for match in NODE_SPAN_PATTERN.findall(text)]


def find_inline_date_refs(text: str) -> list[str]:
    return DATE_SPAN_PATTERN.findall(text)
