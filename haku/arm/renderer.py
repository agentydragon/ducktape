"""Render the Haku dashboard HTML from item dicts.

The Markdown→HTML renderer and per-item card rendering are ported verbatim from
haku-state's ``dashboard/generate.py`` (the static generator). The only change:
``render_page`` returns the HTML string (no disk write) and takes the page
template + CSS as arguments, which the arm loads from its haku-state clone (see
``templates_loader``) so Haku can evolve the look without an image rebuild.

The renderer is intentionally a tiny vendored Markdown subset (paragraphs,
``-``/``*``/numbered lists with indent nesting, ``**bold**``, ``code``, links) —
not a full CommonMark parser — kept byte-compatible with the static generator.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from collections.abc import Collection

import jinja2

# Public Forgejo web URLs for the browser-facing links the operator clicks
# (distinct from the internal git URL the arm uses for git operations).
FORGEJO = "https://git.allegedly.works/haku/haku-state"
INTAKE_NEW = f"{FORGEJO}/_new/main/intake/"
ITEM_SRC = f"{FORGEJO}/src/branch/main/items"
CLAUDE_NEW = "https://claude.ai/new?q="
MAX_DEEPLINK = 2000  # fall back to the item file when the encoded prompt exceeds this

UP_NEXT = 7

_MARKER = re.compile(r"^([-*]|\d+\.)\s+(.*)$")


def _inline(text: str) -> str:
    """Inline Markdown → HTML on a single logical line (HTML-escaped first).

    Code/bold conversion is skipped when the relevant delimiter count is odd, so
    an unbalanced backtick or ``**`` (e.g. a nix error like ``option `x'``) is left
    literal instead of mis-pairing across the rest of the line."""
    text = html.escape(text)
    if text.count("`") % 2 == 0:
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    if text.count("**") % 2 == 0:
        text = re.sub(r"\*\*([^*]+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    # bare links not already inside an href="…"; stop before trailing punctuation
    return re.sub(r'(?<![">=])(https?://[^\s<)]+[\w/])', r'<a href="\1">\1</a>', text)


def _logical_lines(text: str) -> list[tuple[str, int, str]]:
    """Collapse wrapped source lines into logical units (kind, indent, content),
    kind ∈ {p, ul, ol}. A non-marker, non-blank line continues the current unit."""
    out: list[tuple[str, int, list[str]]] = []
    cur: tuple[str, int, list[str]] | None = None
    for raw in text.rstrip("\n").split("\n"):
        if not raw.strip():
            if cur:
                out.append(cur)
                cur = None
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        m = _MARKER.match(raw.strip())
        if m:
            if cur:
                out.append(cur)
            cur = ("ol" if m.group(1)[0].isdigit() else "ul", indent, [m.group(2)])
        elif cur is None:
            cur = ("p", indent, [raw.strip()])
        else:
            cur[2].append(raw.strip())
    if cur:
        out.append(cur)
    return [(kind, indent, " ".join(parts)) for kind, indent, parts in out]


def md_to_html(text: str) -> str:
    parts: list[str] = []
    stack: list[tuple[int, str]] = []  # open lists as (indent, tag)

    def close_below(indent: int) -> None:
        while stack and stack[-1][0] >= indent:
            parts.append(f"</{stack[-1][1]}>")
            stack.pop()

    for kind, indent, content in _logical_lines(text):
        if kind == "p":
            close_below(0)
            parts.append(f"<p>{_inline(content)}</p>")
            continue
        while stack and stack[-1][0] > indent:
            parts.append(f"</{stack[-1][1]}>")
            stack.pop()
        if not stack or stack[-1][0] < indent:
            parts.append(f"<{kind}>")
            stack.append((indent, kind))
        parts.append(f"<li>{_inline(content)}</li>")
    while stack:
        parts.append(f"</{stack[-1][1]}>")
        stack.pop()
    return "\n".join(parts)


def deeplink(item: dict) -> tuple[str, str]:
    """(href, label) for an item's primary action button."""
    action = item.get("action", {})
    if action.get("kind") == "prepared_prompt":
        encoded = urllib.parse.quote(action["prompt"])
        if len(encoded) <= MAX_DEEPLINK:
            return CLAUDE_NEW + encoded, "Hand to Claude →"
    return f"{ITEM_SRC}/{item['id']}.yaml", "Open item →"


def _render_action(item_id: str, action: dict, clicked: bool) -> str:
    """One operator action attached to an item (``actions[]``).

    A ``command`` is a **click/un-click toggle**: the form POSTs to record the
    click (or to ``…/unclick`` to retract it) — the click state lives in the
    clicks/ overlay, not the item, and Haku reduces it on its next run. A
    ``claude_handoff`` is a stateless ``claude.ai/new`` deep-link (no commit)."""
    label = html.escape(action["label"])
    if action["kind"] == "claude_handoff":
        encoded = urllib.parse.quote(action.get("prompt", ""))
        href = CLAUDE_NEW + encoded if len(encoded) <= MAX_DEEPLINK else f"{ITEM_SRC}/{item_id}.yaml"
        return f'<a class="act handoff" href="{html.escape(href)}">{label} →</a>'
    base = f"/items/{urllib.parse.quote(item_id)}/actions/{urllib.parse.quote(action['id'])}"
    target = f"{base}/unclick" if clicked else base
    btn = f'<button class="act{" clicked" if clicked else ""}" type="submit">{label}</button>'
    return f'<form class="act-form" method="post" action="{html.escape(target)}">{btn}</form>'


def _render_actions(item: dict, clicked_ids: Collection[str]) -> str:
    actions = item.get("actions") or []
    if not actions:
        return ""
    cells = "\n".join(_render_action(item["id"], a, a["id"] in clicked_ids) for a in actions)
    return f'<div class="acts">{cells}</div>'


def render_task(item: dict, clicked_ids: Collection[str] = ()) -> str:
    """One collapsible task. Summary = compact row; the body (Markdown→HTML),
    operator action toggles, and primary action button live only inside the
    expanded details view. ``clicked_ids`` are this item's currently-clicked
    action ids (from the clicks/ overlay)."""
    href, label = deeplink(item)
    body = md_to_html(item.get("body", ""))
    deadline = item.get("deadline")
    dl = f'<span class="deadline">⏳ {html.escape(deadline[:10])}</span>' if deadline else ""
    kind = item.get("action", {}).get("kind", "")
    src = item.get("source", "")
    return f"""
    <details class="task">
      <summary>
        <span class="val">{item["value"]}</span>
        <span class="title">{html.escape(item["title"])}</span>
        <span class="meta">{dl}<span class="kind">{html.escape(kind)}</span></span>
      </summary>
      <div class="detail">
        {body}
        {_render_actions(item, clicked_ids)}
        <p class="actions">
          <a class="btn" href="{html.escape(href)}">{label}</a>
          <span class="src">{html.escape(src)}</span>
          <a class="srclink" href="{ITEM_SRC}/{item["id"]}.yaml">item source →</a>
        </p>
      </div>
    </details>"""


def render_page(
    items: list[dict],
    *,
    scan_time: str,
    page_template: jinja2.Template,
    css: str,
    clicks: Collection[tuple[str, str]] = (),
) -> str:
    """Render the full dashboard page. Open items ranked by value: top UP_NEXT in
    "Up next", the rest in a collapsible "Backlog"; both are per-task ``<details>``.
    ``clicks`` are the currently-clicked ``(item_id, action_id)`` pairs (overlay)."""
    by_status: dict[str, int] = {}
    for it in items:
        by_status[it["status"]] = by_status.get(it["status"], 0) + 1

    clicked_by_item: dict[str, set[str]] = {}
    for item_id, action_id in clicks:
        clicked_by_item.setdefault(item_id, set()).add(action_id)

    def task(it: dict) -> str:
        return render_task(it, clicked_by_item.get(it["id"], frozenset()))

    open_items = sorted((it for it in items if it["status"] == "open"), key=lambda it: it["value"], reverse=True)
    up_next, backlog = open_items[:UP_NEXT], open_items[UP_NEXT:]
    up_next_html = "\n".join(task(it) for it in up_next) or "<p>No open items.</p>"
    backlog_html = (
        f"""
    <details class="backlog">
      <summary>Backlog — {len(backlog)} more open item(s)</summary>
      {"".join(task(it) for it in backlog)}
    </details>"""
        if backlog
        else ""
    )
    counts = " · ".join(f"{k}: {v}" for k, v in sorted(by_status.items()))
    return page_template.render(
        css=css,
        intake_new=INTAKE_NEW,
        up_next_html=up_next_html,
        backlog_html=backlog_html,
        open_count=len(open_items),
        counts=counts,
        scan_time=scan_time,
    )
