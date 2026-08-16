"""Render Haku's Markdown into the HTML subset Matrix clients will actually display.

Haku writes Markdown because that is what models write well; Element shows `body` verbatim,
so without this a reply arrives with its asterisks and backticks intact. The conversion lives
here rather than in the prompt for the reason recorded in <../../../../plans/matrix_chat_runtime.md>
R11.7: formatting is a property of the surface, not a choice the agent makes.

Everything not on the spec's allowlist is dropped, and the dropping is the point — a tag
Element would strip anyway is better removed here, where the fallback is deliberate, than
silently at the far end.
"""

from __future__ import annotations

import re

import markdown
from bs4 import BeautifulSoup, NavigableString, Tag

# https://spec.matrix.org/latest/client-server-api/#mroommessage-msgtypes — the tags a client
# is required to understand. Anything outside it is unrenderable somewhere, so it is unwrapped
# to its text rather than sent and hoped for.
_ALLOWED_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "br",
        "caption",
        "code",
        "del",
        "details",
        "div",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "img",
        "li",
        "ol",
        "p",
        "pre",
        "s",
        "span",
        "strong",
        "sub",
        "summary",
        "sup",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
)

# Per-tag, because the spec allowlists attributes per tag rather than globally. Omitted tags
# keep no attributes at all — notably no `id`, `style`, or general `class`.
_ALLOWED_ATTRIBUTES = {
    "span": frozenset({"data-mx-bg-color", "data-mx-color", "data-mx-spoiler", "data-mx-maths"}),
    "a": frozenset({"target", "href"}),
    "img": frozenset({"width", "height", "alt", "title", "src"}),
    "ol": frozenset({"start"}),
    "code": frozenset({"class"}),
    "div": frozenset({"data-mx-maths"}),
}

_ALLOWED_SCHEMES = ("https:", "http:", "ftp:", "mailto:", "magnet:", "matrix:")

# GitHub-flavoured task lists have no Matrix equivalent: `<input type="checkbox">` is not on the
# allowlist, so a checklist would arrive as bare bullets with the boxes silently gone. Haku
# writes checklists often enough that losing their state is worse than losing the widget.
_TASK_ITEM = re.compile(r"^(\s*[-*+]\s+)\[([ xX])\]\s+", re.MULTILINE)


def _checkboxes_to_text(text: str) -> str:
    return _TASK_ITEM.sub(lambda m: f"{m.group(1)}{'☑' if m.group(2) in 'xX' else '☐'} ", text)


def _permitted_attributes(tag: Tag) -> dict[str, str]:
    allowed = _ALLOWED_ATTRIBUTES.get(tag.name, frozenset())
    kept: dict[str, str] = {}
    for name, value in tag.attrs.items():
        if name not in allowed:
            continue
        # BeautifulSoup gives multi-valued attributes (`class`) as a list.
        text = " ".join(value) if isinstance(value, list) else str(value)
        if name == "href" and not text.startswith(_ALLOWED_SCHEMES):
            continue
        # Only `mxc://` is displayable: an https image is dropped by the client anyway, and
        # fetching it would leak the room's readers to whoever serves it.
        if name == "src" and not text.startswith("mxc://"):
            continue
        if name == "class":
            text = " ".join(word for word in text.split() if word.startswith("language-"))
            if not text:
                continue
        kept[name] = text
    return kept


def _sanitize(soup: BeautifulSoup) -> None:
    """Strip what Matrix does not allow, keeping the text inside it.

    `unwrap` rather than `decompose`: an unknown tag is a formatting failure, not a reason to
    lose the words. Raw HTML the agent typed reaches here as real tags — Markdown passes it
    through — which is exactly why the allowlist is applied to the output rather than trusted
    from the input.
    """
    for tag in soup.find_all(True):
        if tag.name == "img" and not str(tag.get("src", "")).startswith("mxc://"):
            # No text to keep, and `unwrap` would leave nothing; prefer the alt text.
            tag.replace_with(NavigableString(str(tag.get("alt", "")).strip()))
            continue
        if tag.name not in _ALLOWED_TAGS:
            tag.unwrap()
            continue
        # Cleared and refilled rather than rebound: `Tag.attrs` admits multi-valued attributes,
        # so it is invariant against the plain `dict[str, str]` this builds.
        permitted = _permitted_attributes(tag)
        tag.attrs.clear()
        tag.attrs.update(permitted)


def to_formatted_body(body: str) -> str | None:
    """The `formatted_body` for *body*, or None when the plain text already says it all.

    None rather than the escaped source: a message whose rendering adds nothing should not
    carry a second copy of itself, and clients fall back to `body` when the field is absent.
    """
    html = markdown.markdown(
        _checkboxes_to_text(body), extensions=["fenced_code", "tables", "sane_lists"], output_format="html"
    )
    soup = BeautifulSoup(html, "html.parser")
    _sanitize(soup)
    formatted = soup.decode().strip()

    # A single paragraph of exactly the original text is what plain Markdown prose becomes,
    # and `<p>` around it is not formatting worth a second field.
    if formatted == f"<p>{body.strip()}</p>":
        return None
    return formatted or None
