"""Conversions between the fold's records and the entity rows that store them, in both directions.

Nothing here touches the database."""

from __future__ import annotations

from uuid import UUID

from google.protobuf.json_format import MessageToDict

from agentplane.app.agent_runtime.view import fold
from agentplane.app.agent_runtime.view.views import (
    EntityKind,
    ThreadCommandEntityView,
    ThreadCommandState,
    ThreadConfirmedInputEntityView,
    ThreadConfirmedInputState,
    ThreadControlsState,
    ThreadEntityView,
    ThreadItemEntityView,
    ThreadItemState,
    ThreadLifecycleEntityView,
    ThreadLifecycleState,
    ThreadOperationalState,
    ThreadPayloadReference,
    ThreadViewState,
    ThreadViewStateEntityView,
)


def fold_item(view: ThreadItemEntityView) -> fold.Item:
    return fold.Item(
        view.projection_epoch,
        view.entity_id,
        view.cursor,
        view.revision_cursor,
        kind=view.state.kind,
        tool_name=view.state.tool_name,
        turn_id=view.turn_id,
        text=_fold_ref(view.text_ref),
        arguments=_fold_ref(view.arguments_ref),
        output=_fold_ref(view.output_ref),
        completion=_fold_completion(view.state),
    )


def _fold_completion(state: ThreadItemState) -> fold.Completion | None:
    match state.completion, state.tool_succeeded:
        case None, None:
            return None
        case "text", None:
            return fold.TextCompletion()
        case "tool", bool(succeeded):
            return fold.ToolCompletion(succeeded)
    raise ValueError(f"invalid item completion: {state.completion=} {state.tool_succeeded=}")


def command_summary(view: ThreadCommandEntityView) -> fold.CommandSummary:
    return fold.CommandSummary(
        view.projection_epoch,
        view.entity_id,
        view.cursor,
        view.state.operation,
        view.state.outcome,
        None if view.state.outcome_cursor is None else int(view.state.outcome_cursor),
        view.state.outcome_reason,
        _fold_ref(view.input_ref),
    )


def _fold_ref(reference: ThreadPayloadReference | None) -> fold.PayloadRef | None:
    if reference is None:
        return None
    return fold.PayloadRef(
        reference.projection_epoch,
        int(reference.owner_cursor),
        reference.owner_id,
        reference.field,
        int(reference.revision_cursor),
        int(reference.generation),
    )


def ordered_entity_rows(
    thread_id: UUID, result: fold.ProjectionBatch, operational: ThreadOperationalState | None = None
) -> list[dict[str, object]]:
    """A batch's rows in thread order, which is the order they are numbered.

    The recorder numbers rows in the order it inserts them, so a batch inserting in upsert order
    would number its rows in that order rather than the reader's. Kind and identity break a tie, so
    a batch numbers the same rows the same way however it was assembled.
    """
    return sorted(
        _entity_rows(thread_id, result, operational),
        key=lambda row: (row["cursor"], row["entity_kind"], row["entity_id"]),
    )


def _entity_rows(
    thread_id: UUID, result: fold.ProjectionBatch, operational: ThreadOperationalState | None = None
) -> list[dict[str, object]]:
    entities: list[ThreadEntityView] = [_view_state_entity(thread_id, result.state, operational)]
    entities.extend(_item_entity(thread_id, item) for item in result.item_upserts)
    entities.extend(_confirmed_input_entity(thread_id, value) for value in result.confirmed_input_upserts)
    entities.extend(_lifecycle_entity(thread_id, value) for value in result.lifecycle_upserts)
    entities.extend(_command_entity(thread_id, value) for value in result.command_upserts)
    return [entity.model_dump(mode="json") for entity in entities]


def _view_state_entity(
    thread_id: UUID, state: fold.ViewState, operational: ThreadOperationalState | None = None
) -> ThreadViewStateEntityView:
    return ThreadViewStateEntityView(
        thread_id=thread_id,
        projection_epoch=state.position.projection_epoch,
        entity_kind=EntityKind.VIEW_STATE,
        entity_id="current",
        cursor=state.position.through_cursor,
        revision_cursor=state.position.through_cursor,
        pending=False,
        turn_id=None,
        state=ThreadViewState(
            controls=ThreadControlsState(
                applied_model=state.controls.applied_model,
                active_turn_id=state.controls.active_turn_id,
                harness_state=state.controls.harness_state,
            ),
            operational=operational
            or ThreadOperationalState(
                status="active", last_verified_cursor=str(state.position.through_cursor), feed_error=None
            ),
        ),
        text_ref=None,
        arguments_ref=None,
        output_ref=None,
        input_ref=None,
    )


def _item_entity(thread_id: UUID, item: fold.Item) -> ThreadItemEntityView:
    tool = item.completion if isinstance(item.completion, fold.ToolCompletion) else None
    return ThreadItemEntityView(
        thread_id=thread_id,
        projection_epoch=item.projection_epoch,
        entity_kind=EntityKind.ITEM,
        entity_id=item.item_id,
        cursor=item.cursor,
        revision_cursor=item.revision_cursor,
        pending=False,
        turn_id=item.turn_id,
        state=ThreadItemState(
            kind=item.kind,
            tool_name=item.tool_name,
            completion=None if item.completion is None else "tool" if tool is not None else "text",
            tool_succeeded=None if tool is None else tool.succeeded,
        ),
        text_ref=_reference(item.text),
        arguments_ref=_reference(item.arguments),
        output_ref=_reference(item.output),
        input_ref=None,
    )


def _confirmed_input_entity(thread_id: UUID, value: fold.ConfirmedInput) -> ThreadConfirmedInputEntityView:
    return ThreadConfirmedInputEntityView(
        thread_id=thread_id,
        projection_epoch=value.projection_epoch,
        entity_kind=EntityKind.CONFIRMED_INPUT,
        entity_id=str(value.cursor),
        cursor=value.cursor,
        revision_cursor=value.revision_cursor,
        pending=False,
        turn_id=value.turn_id,
        state=ThreadConfirmedInputState(
            harness_message_id=value.harness_message_id, origin_command_ids=list(value.origin_command_ids)
        ),
        text_ref=None,
        arguments_ref=None,
        output_ref=None,
        input_ref=_reference(value.text),
    )


def _lifecycle_entity(thread_id: UUID, value: fold.LifecycleSegment) -> ThreadLifecycleEntityView:
    return ThreadLifecycleEntityView(
        thread_id=thread_id,
        projection_epoch=value.projection_epoch,
        entity_kind=EntityKind.LIFECYCLE,
        entity_id=str(value.cursor),
        cursor=value.cursor,
        revision_cursor=value.revision_cursor,
        pending=False,
        turn_id=None,
        state=ThreadLifecycleState(observation=value.observation, event=MessageToDict(value.event)),
        text_ref=None,
        arguments_ref=None,
        output_ref=None,
        input_ref=None,
    )


def _command_entity(thread_id: UUID, value: fold.CommandSummary) -> ThreadCommandEntityView:
    return ThreadCommandEntityView(
        thread_id=thread_id,
        projection_epoch=value.projection_epoch,
        entity_kind=EntityKind.COMMAND,
        entity_id=value.command_id,
        cursor=value.admission_cursor,
        revision_cursor=value.outcome_cursor or value.admission_cursor,
        pending=value.outcome is fold.CommandOutcome.PENDING,
        turn_id=None,
        state=ThreadCommandState(
            operation=value.operation,
            outcome=value.outcome,
            outcome_cursor=None if value.outcome_cursor is None else str(value.outcome_cursor),
            outcome_reason=value.outcome_reason,
        ),
        text_ref=None,
        arguments_ref=None,
        output_ref=None,
        input_ref=_reference(value.input),
    )


def _reference(reference: fold.PayloadRef | None) -> ThreadPayloadReference | None:
    if reference is None:
        return None
    return ThreadPayloadReference(
        projection_epoch=reference.projection_epoch,
        owner_cursor=str(reference.owner_cursor),
        owner_id=reference.owner_id,
        field=reference.field,
        revision_cursor=str(reference.revision_cursor),
        generation=str(reference.generation),
    )
