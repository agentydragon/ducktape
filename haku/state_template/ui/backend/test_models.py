"""Item parsing — the discriminated-union schema the UI reads from haku-state.

The bug class these guard: a malformed item silently parsing into a wrong shape, or a
new top-level item field breaking a not-yet-rebuilt UI. Both are real (items/ is
hand/Haku-authored YAML), so the dashboard read must reject invalid enums/action kinds
and tolerate unknown fields.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from models import (
    ClaudeHandoffAction,
    CommandAction,
    Friction,
    ImprovementIdea,
    ImprovementsBoard,
    Item,
    ItemStatus,
    PreparedPrompt,
)

# A realistic item as it appears in items/<id>.yaml, including the top-level fields the
# UI model does NOT declare (dedup_key, source) — those must be ignored, not rejected.
_ITEM_YAML = """
id: 01EXAMPLE
dedup_key: some-dedup-key
source: cluster_check
title: "A thing worth surfacing"
body: |
  Multi-line markdown body.
value: 70
status: open
deadline: 2026-07-01T17:00:00-07:00
actions:
  - id: done
    label: "Mark done"
    kind: command
    intent: "Set status=done."
  - id: handoff
    label: "Hand to Claude"
    kind: claude_handoff
    prompt: "Go do the thing."
"""


def test_parses_full_item_and_ignores_unknown_fields():
    item = Item.model_validate(yaml.safe_load(_ITEM_YAML))
    assert item.id == "01EXAMPLE"
    assert item.status is ItemStatus.OPEN
    assert item.value == 70
    assert item.deadline is not None
    assert item.deadline.year == 2026
    # dedup_key / source are not Item fields — ignored, not stored, not an error.
    assert not hasattr(item, "dedup_key")


def test_actions_resolve_to_their_discriminated_types():
    item = Item.model_validate(yaml.safe_load(_ITEM_YAML))
    assert isinstance(item.actions[0], CommandAction)
    assert isinstance(item.actions[1], ClaudeHandoffAction)
    assert item.actions[1].prompt == "Go do the thing."


def test_minimal_item_has_no_action_and_no_deadline():
    item = Item.model_validate({"id": "x", "title": "t", "body": "b", "value": 1, "status": "done"})
    assert item.action is None
    assert item.deadline is None
    assert item.actions == []


def test_prepared_prompt_primary_action():
    item = Item.model_validate(
        {
            "id": "x",
            "title": "t",
            "body": "b",
            "value": 1,
            "status": "open",
            "action": {"kind": "prepared_prompt", "prompt": "do it"},
        }
    )
    assert isinstance(item.action, PreparedPrompt)
    assert item.action.prompt == "do it"


def test_invalid_status_is_rejected():
    with pytest.raises(ValidationError):
        Item.model_validate({"id": "x", "title": "t", "body": "b", "value": 1, "status": "bogus"})


def test_unknown_action_kind_is_rejected():
    with pytest.raises(ValidationError):
        Item.model_validate(
            {
                "id": "x",
                "title": "t",
                "body": "b",
                "value": 1,
                "status": "open",
                "actions": [{"id": "a", "label": "l", "kind": "nope"}],
            }
        )


def test_command_action_requires_intent():
    with pytest.raises(ValidationError):
        Item.model_validate(
            {
                "id": "x",
                "title": "t",
                "body": "b",
                "value": 1,
                "status": "open",
                "actions": [{"id": "a", "label": "l", "kind": "command"}],
            }
        )


# --- improvements / friction backlog ------------------------------------------


def test_improvement_idea_rejects_bad_value():
    with pytest.raises(ValidationError):
        ImprovementIdea(id="x", title="t", value="huge", status="idea", summary="s")


def test_friction_rejects_bad_status():
    with pytest.raises(ValidationError):
        Friction(id="x", title="t", severity="high", status="ongoing", detail="d")


_IMPROVEMENTS_YAML = """
updated: "2026-06-29T05:30:00Z"
ideas:
  - id: tana-translog-pipe
    title: "Direct Tana change-feed"
    value: high
    status: recommend
    summary: "lossless change stream"
    detail: |
      **why** it matters
friction:
  - id: github-token-sops
    title: "GITHUB_TOKEN sops decrypt fails"
    severity: medium
    status: open
    detail: "add haku recipient"
"""


def test_improvements_board_parses_ideas_and_friction():
    board = ImprovementsBoard.model_validate(yaml.safe_load(_IMPROVEMENTS_YAML))
    assert [i.id for i in board.ideas] == ["tana-translog-pipe"]
    assert board.ideas[0].value == "high"
    assert board.ideas[0].status == "recommend"
    assert [f.id for f in board.friction] == ["github-token-sops"]
    assert board.friction[0].severity == "medium"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
