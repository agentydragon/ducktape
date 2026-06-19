"""Unit tests for the ported Markdown→HTML renderer and page assembly."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel

from haku.console import renderer, templates_loader
from haku.console.models import (
    ClaudeHandoffAction,
    CommandAction,
    Item,
    ItemStatus,
    PreparedPrompt,
    PrimaryAction,
    Suggestion,
)

_NOWHERE = Path("/nonexistent-clone")  # no override → baked template/css


def _item(
    idx: int,
    value: int,
    *,
    status: ItemStatus = ItemStatus.OPEN,
    action: PrimaryAction | None = None,
    deadline: str | None = None,
    actions: list[CommandAction | ClaudeHandoffAction] | None = None,
) -> Item:
    return Item(
        id=f"01{idx:024d}",
        title=f"Item {idx}",
        value=value,
        source="test",
        status=status,
        body="**why** it matters with `code`.\n\n- one\n- two",
        action=action or Suggestion(),
        deadline=deadline,
        actions=actions or [],
    )


def _page(items: list[Item]) -> str:
    return renderer.render_page(
        items,
        scan_time="2026-01-01 00:00 UTC",
        page_template=templates_loader.load_page_template(_NOWHERE),
        css=templates_loader.load_css(_NOWHERE),
    )


def test_md_bold_code_and_list() -> None:
    out = renderer.md_to_html("intro line\n\n- **a** and `b`\n- second")
    assert "<p>intro line</p>" in out
    assert "<strong>a</strong>" in out
    assert "<code>b</code>" in out
    assert out.count("<li>") == 2
    assert "<ul>" in out
    assert "</ul>" in out


def test_md_link() -> None:
    assert '<a href="https://x.com/y">t</a>' in renderer._inline("[t](https://x.com/y)")


def test_md_unbalanced_backtick_left_literal() -> None:
    # odd backtick count is left literal rather than mis-paired across the line
    assert "<code>" not in renderer._inline("the `option' does not exist")


def test_deeplink_prepared_prompt() -> None:
    href, label = renderer.deeplink(_item(0, 50, action=PreparedPrompt(prompt="go")))
    assert href.startswith(renderer.CLAUDE_NEW)
    assert label == "Hand to Claude →"


def test_deeplink_suggestion() -> None:
    href, label = renderer.deeplink(_item(0, 50, action=Suggestion()))
    assert href.endswith(".yaml")
    assert label == "Open item →"


def test_render_task_summary_body_deadline() -> None:
    rendered = renderer.render_task(
        _item(1, 92, action=PreparedPrompt(prompt="do the thing"), deadline="2026-07-01T17:00:00Z")
    )
    assert 'class="val">92<' in rendered
    assert "Item 1" in rendered
    assert "Hand to Claude →" in rendered
    assert "<strong>why</strong>" in rendered
    assert "⏳ 2026-07-01" in rendered


def test_render_page_tiers_and_counts() -> None:
    items = [_item(i, 100 - i) for i in range(9)] + [_item(99, 10, status=ItemStatus.REJECTED)]
    page = _page(items)
    assert "<!doctype html>" in page
    assert "Up next" in page
    # 9 open → 7 up-next + 2 in the backlog <details>
    assert "Backlog — 2 more open item(s)" in page
    assert page.count('<details class="task">') == 9  # all open items render as tasks
    assert "open: 9" in page
    assert "rejected: 1" in page
    assert "Last scan: 2026-01-01 00:00 UTC" in page


def test_render_page_empty() -> None:
    assert "No open items." in _page([])


_SNOOZE = CommandAction(id="snooze", label="Snooze 30d", intent="snooze 30d")


def test_command_action_unclicked_renders_post_toggle() -> None:
    item = _item(1, 50, actions=[_SNOOZE])
    rendered = renderer.render_task(item)
    assert f'action="/items/{item.id}/actions/snooze"' in rendered
    assert "/unclick" not in rendered
    assert 'class="act"' in rendered  # not the clicked variant


def test_command_action_clicked_renders_unclick_toggle() -> None:
    item = _item(1, 50, actions=[_SNOOZE])
    rendered = renderer.render_task(item, clicked_ids={"snooze"})
    assert f'action="/items/{item.id}/actions/snooze/unclick"' in rendered
    assert 'class="act clicked"' in rendered


def test_claude_handoff_action_is_stateless_link() -> None:
    handoff = ClaudeHandoffAction(id="draft", label="Draft the email", prompt="write it")
    rendered = renderer.render_task(_item(1, 50, actions=[handoff]))
    assert renderer.CLAUDE_NEW in rendered
    assert "<form" not in rendered  # handoff is a link, never a click-toggle


def test_render_page_threads_clicks_to_the_right_item() -> None:
    clicked, other = _item(1, 50, actions=[_SNOOZE]), _item(2, 40, actions=[_SNOOZE])
    page = renderer.render_page(
        [clicked, other],
        scan_time="t",
        page_template=templates_loader.load_page_template(_NOWHERE),
        css="",
        clicks={(clicked.id, "snooze")},
    )
    assert f'action="/items/{clicked.id}/actions/snooze/unclick"' in page  # clicked → unclick
    assert f'action="/items/{other.id}/actions/snooze"' in page  # untouched → plain click


if __name__ == "__main__":
    pytest_bazel.main()
