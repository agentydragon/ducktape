from __future__ import annotations

from hamcrest import all_of, assert_that, has_item, has_properties, instance_of

from agent_server.server.state import AssistantMarkdownItem, DisplayItem, EndTurnItem, UiState, UserMessageItem


def item_user_message(text: str | None = None):
    m = [instance_of(UserMessageItem)]
    if text is not None:
        m.append(has_properties(text=text))
    return all_of(*m)


def item_assistant_markdown(md: str | None = None):
    m = [instance_of(AssistantMarkdownItem)]
    if md is not None:
        m.append(has_properties(md=md))
    return all_of(*m)


def item_end_turn():
    return instance_of(EndTurnItem)


def assert_ui_items_have(items: list[DisplayItem], *matchers):
    for m in matchers:
        assert_that(items, has_item(m))


def assert_ui_state_has(ui_state: dict, *matchers):
    # Validate and coerce to typed UiState so matchers can assert on types
    s = UiState.model_validate(ui_state)
    assert_ui_items_have(s.items, *matchers)
