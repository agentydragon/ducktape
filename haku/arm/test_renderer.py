"""Unit tests for the ported Markdown→HTML renderer and page assembly."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel

from haku.arm import renderer, templates_loader

_NOWHERE = Path("/nonexistent-clone")  # no override → baked template/css


def _item(idx: int, value: int, *, status: str = "open", kind: str = "suggestion", deadline: str | None = None) -> dict:
    action = {"kind": kind} if kind == "suggestion" else {"kind": kind, "prompt": "do the thing"}
    item = {
        "id": f"01{idx:024d}",
        "dedup_key": f"dk-{idx}",
        "title": f"Item {idx}",
        "value": value,
        "source": "test",
        "status": status,
        "body": "**why** it matters with `code`.\n\n- one\n- two",
        "action": action,
    }
    if deadline:
        item["deadline"] = deadline
    return item


def _page(items: list[dict]) -> str:
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
    href, label = renderer.deeplink({"id": "01" + "0" * 24, "action": {"kind": "prepared_prompt", "prompt": "go"}})
    assert href.startswith(renderer.CLAUDE_NEW)
    assert label == "Hand to Claude →"


def test_deeplink_suggestion() -> None:
    href, label = renderer.deeplink({"id": "01" + "0" * 24, "action": {"kind": "suggestion"}})
    assert href.endswith(".yaml")
    assert label == "Open item →"


def test_render_task_summary_body_deadline() -> None:
    rendered = renderer.render_task(_item(1, 92, kind="prepared_prompt", deadline="2026-07-01T17:00:00Z"))
    assert 'class="val">92<' in rendered
    assert "Item 1" in rendered
    assert "Hand to Claude →" in rendered
    assert "<strong>why</strong>" in rendered
    assert "⏳ 2026-07-01" in rendered


def test_render_page_tiers_and_counts() -> None:
    items = [_item(i, 100 - i) for i in range(9)] + [_item(99, 10, status="rejected")]
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


if __name__ == "__main__":
    pytest_bazel.main()
