"""tana Action presentation examples."""

import pytest
import pytest_bazel

from agentplane.app.frontend.actions.testing.action_presentation_cases import ActionExample, ActionPresentationCases

pytest_plugins = ("testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _tana_example(
    name: str,
    args: dict[str, object],
    result: object,
    result_text: str,
    *,
    nodes: tuple[tuple[str, str], ...] = (),
    workspace: bool = False,
    expands: bool = True,
) -> ActionExample:
    summaries: dict[str, object] = {}
    if nodes:
        summaries["nodes"] = [{"nodeId": node_id, "name": label} for node_id, label in nodes]
    if workspace:
        summaries["workspace"] = {"workspaceId": "product", "name": "Product workspace"}
    live_label = nodes[0][1] if nodes else "Product workspace"
    return ActionExample(
        "tana",
        name,
        args,
        result,
        result_text,
        compact_card_expands=expands,
        live_preview=("tana-node-summaries", summaries, live_label) if summaries else None,
    )


_TASK = ("task-1", "Plan review")

_TASK_TAG = ("tag-1", "Task")

_STATUS = ("field-1", "Status")

_DONE = ("option-1", "Done")

_ACTIVE = ("option-2", "Active")


class TestActionPresentations(ActionPresentationCases):
    examples = (
        ActionExample(
            "tana",
            "get_or_create_calendar_node",
            {"workspaceId": "workspace-123", "granularity": "week", "date": "2026-10-12"},
            '{"nodeId":"calendar-week-42"}',
            "Calendar node for week containing 2026-10-12",
            compact_card_expands=False,
            live_preview=(
                "tana-node-summaries",
                {"workspace": {"workspaceId": "workspace-123", "name": "Product workspace"}},
                "Product workspace",
            ),
        ),
        ActionExample(
            "tana",
            "move_node",
            {"nodeId": "draft", "targetNodeId": "website", "position": "start", "keepSourceReference": True},
            'Moved "Draft notes" from "Planning inbox" to "Website project" (start)\n\n'
            "Node ID: draft\n"
            "Previous parent: Planning inbox (inbox)\n"
            "New parent: Website project (website)\n"
            "Position: start",
            "Moved Draft notes from Planning inbox to Website project (start).",
            live_preview=(
                "tana-node-summaries",
                {
                    "nodes": [
                        {"nodeId": "draft", "name": "Draft notes"},
                        {"nodeId": "website", "name": "Website project"},
                    ]
                },
                "Website project",
            ),
        ),
        _tana_example(
            "read_node",
            {"nodeId": "task-1", "maxDepth": 2},
            "- [ ] Plan review <!-- node-id: task-1 -->\n  - Review the milestones <!-- node-id: child-1 -->",
            "Review the milestones",
            nodes=(_TASK,),
        ),
        _tana_example(
            "get_children",
            {"nodeId": "task-1", "offset": 1, "limit": 1},
            {
                "children": [
                    {
                        "id": "child-1",
                        "name": "Milestone checklist",
                        "tags": [{"id": "tag-1", "name": "Task"}],
                        "tagIds": ["tag-1"],
                        "childCount": 2,
                        "docType": "content",
                        "description": "Check the remaining milestones.",
                        "created": "2026-10-10T10:00:00.000Z",
                        "inTrash": False,
                    }
                ],
                "total": 4,
                "hasMore": True,
            },
            "More child nodes available",
            nodes=(_TASK,),
        ),
        _tana_example(
            "open_node",
            {"nodeId": "task-1", "openType": "panel"},
            'Opened "Plan review" in Tana (panel)',
            "Opened Plan review in a new side panel",
            nodes=(_TASK,),
        ),
        _tana_example(
            "list_workspaces",
            {},
            [{"id": "product", "name": "Product workspace", "homeNodeId": "home-1"}],
            "1 loaded workspace",
            expands=False,
        ),
        _tana_example(
            "list_tags",
            {"workspaceId": "product", "limit": 25},
            [{"id": "tag-1", "name": "Task", "color": "blue"}, {"id": "tag-2", "name": "Milestone"}],
            "2 tags returned",
            workspace=True,
        ),
        _tana_example(
            "get_tag_schema",
            {"tagId": "tag-1", "includeEditInstructions": True, "includeInheritedFields": True},
            "# Tag definition: Task (id:tag-1)\n\n## Template Fields\n"
            "- **Status** (id:field-1):: Options = Active *[default]*\n"
            "  - Active (id:option-2) *[default]*\n  - Done (id:option-1)\n\n"
            "## Tag-Level Edit Instructions\nUpdate status after review.\n",
            "Options = Active",
            nodes=(_TASK_TAG,),
        ),
        _tana_example(
            "edit_node",
            {"nodeId": "task-1", "name": {"old_string": "Plan", "new_string": "Schedule"}},
            "Successfully edited node task-1\nName: Schedule review",
            "Edited Schedule review",
            nodes=(_TASK,),
        ),
        _tana_example(
            "trash_node",
            {"nodeId": "task-1"},
            'Moved "Plan review" to trash\n\nNode ID: task-1\nTrash ID: trash-1',
            "Moved Plan review to trash",
            nodes=(_TASK,),
        ),
        _tana_example(
            "check_node", {"nodeId": "task-1"}, 'Checked "Plan review"', "Completed Plan review", nodes=(_TASK,)
        ),
        _tana_example(
            "uncheck_node", {"nodeId": "task-1"}, 'Unchecked "Plan review"', "Reopened Plan review", nodes=(_TASK,)
        ),
        _tana_example(
            "tag",
            {"nodeId": "task-1", "action": "add", "tagIds": ["tag-1"]},
            'Adding tags on "Plan review" (task-1):\n✓ Task: Added tag "Task"',
            'Added tag "Task"',
            nodes=(_TASK, _TASK_TAG),
        ),
        _tana_example(
            "set_tag_checkbox",
            {
                "tagId": "tag-1",
                "showCheckbox": True,
                "doneStateMapping": {
                    "fieldId": "field-1",
                    "checkedValues": ["option-1"],
                    "uncheckedValues": ["option-2"],
                },
            },
            '✅ Configured checkbox for tag "Task"\nCheckbox: enabled\nDone state mapping: configured',
            "Checkbox enabled for Task",
            nodes=(_TASK_TAG, _STATUS, _DONE, _ACTIVE),
        ),
        _tana_example(
            "create_tag",
            {
                "workspaceId": "product",
                "name": "Milestone",
                "description": "A checkpoint for a project.",
                "extendsTagIds": ["tag-1"],
                "showCheckbox": True,
            },
            '✅ Created tag "Milestone" extending Task\nTag ID: tag-2\nExtends: Task',
            "Created tag Milestone",
            nodes=(_TASK_TAG,),
            workspace=True,
        ),
        _tana_example(
            "add_field_to_tag",
            {
                "tagId": "tag-1",
                "name": "Priority",
                "dataType": "options",
                "options": ["Standard", "Urgent"],
                "defaultValue": "Standard",
            },
            '✅ Added field "Priority" to tag "Task"\nField ID: field-2\nData Type: options',
            "Added Priority (options) to Task",
            nodes=(_TASK_TAG,),
        ),
        _tana_example(
            "set_field_content",
            {"nodeId": "task-1", "attributeId": "field-2", "content": None},
            "Field cleared",
            "Field cleared",
            nodes=(_TASK, ("field-2", "Notes")),
        ),
        _tana_example(
            "set_field_option",
            {"nodeId": "task-1", "attributeId": "field-1", "optionId": "option-1", "mode": "append"},
            'Appended field to option "Done"',
            "Appended to field Done",
            nodes=(_TASK, _STATUS, _DONE),
        ),
        _tana_example(
            "import_tana_paste",
            {
                "parentNodeId": "task-1",
                "content": "%%tana%%\n- Project kickoff #Task\n  - Owner:: Product\n  - Prepare the brief",
            },
            'Successfully imported 1 node(s) under "Plan review"\n\nTarget container: container-1\n'
            'Created nodes:\n- kickoff-1 ("Project kickoff")',
            "Imported 1 node under Plan review",
            nodes=(_TASK,),
        ),
    )


if __name__ == "__main__":
    pytest_bazel.main()
