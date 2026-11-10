package threadfold

import (
	"cmp"
	"errors"
	"fmt"
	"slices"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/google/uuid"
)

// PayloadReferences returns every payload reference a batch's rows store.
func PayloadReferences(result ProjectionBatch) []PayloadRef {
	var refs []PayloadRef
	for _, item := range result.ItemUpserts {
		for _, ref := range []*PayloadRef{item.Text, item.Arguments, item.Output} {
			if ref != nil {
				refs = append(refs, *ref)
			}
		}
	}
	for _, input := range result.ConfirmedInputUpserts {
		refs = append(refs, input.Text)
	}
	for _, command := range result.CommandUpserts {
		if command.Input != nil {
			refs = append(refs, *command.Input)
		}
	}
	return refs
}

// OrderedEntityRows constructs protobuf entities in the order the recorder assigns indices.
func OrderedEntityRows(id uuid.UUID, result ProjectionBatch, extents map[PayloadRef]uint64, operational *historypb.ThreadOperational) ([]*historypb.ThreadEntity, error) {
	var buildErr error
	ref := func(r *PayloadRef) *historypb.ThreadPayloadReference {
		if r == nil {
			return nil
		}
		chunks, ok := extents[*r]
		if !ok {
			buildErr = errors.Join(buildErr, fmt.Errorf("no chunk count for payload %s of %s at revision %d", r.Field, r.OwnerID, r.RevisionCursor))
		}
		buildErr = errors.Join(buildErr, validatePayloadField(r.Field))
		return &historypb.ThreadPayloadReference{ProjectionEpoch: r.ProjectionEpoch, OwnerCursor: r.OwnerCursor, OwnerId: r.OwnerID, Field: r.Field, RevisionCursor: r.RevisionCursor, Generation: r.Generation, ChunkCount: chunks}
	}
	entity := func(epoch, eid string, cursor, revision uint64) *historypb.ThreadEntity {
		return &historypb.ThreadEntity{SessionId: id.String(), ProjectionEpoch: epoch, EntityId: eid, Cursor: cursor, RevisionCursor: revision}
	}
	through := result.State.Position.ThroughCursor
	if operational == nil {
		operational = &historypb.ThreadOperational{Status: historypb.ThreadFeedStatus_THREAD_FEED_STATUS_ACTIVE, LastVerifiedCursor: through}
	}
	buildErr = errors.Join(buildErr, ValidateOperational(operational))
	control := result.State.Controls
	view := entity(result.State.Position.ProjectionEpoch, "current", through, through)
	view.Entity = &historypb.ThreadEntity_ViewState{ViewState: &historypb.ThreadViewState{Controls: &historypb.ThreadControls{AppliedModel: control.AppliedModel, AppliedReasoningEffort: control.AppliedReasoningEffort, ActiveTurnId: control.ActiveTurnID, HarnessState: control.HarnessState}, Operational: operational}}
	rows := []*historypb.ThreadEntity{view}
	for _, item := range result.ItemUpserts {
		e := entity(item.ProjectionEpoch, item.ItemID, item.Cursor, item.RevisionCursor)
		v := &historypb.ThreadItem{Kind: item.Kind, ToolName: item.ToolName, Recovery: item.Recovery, RecoveryReason: item.RecoveryReason, TurnId: item.TurnID, TextRef: ref(item.Text), ArgumentsRef: ref(item.Arguments), OutputRef: ref(item.Output)}
		switch c := item.Completion.(type) {
		case TextCompletion:
			v.Completion = &historypb.ThreadItem_Text{Text: &historypb.ThreadItemTextCompletion{}}
		case ToolCompletion:
			v.Completion = &historypb.ThreadItem_Tool{Tool: &historypb.ThreadItemToolCompletion{Succeeded: c.Succeeded}}
		}
		e.Entity = &historypb.ThreadEntity_Item{Item: v}
		rows = append(rows, e)
	}
	for _, input := range result.ConfirmedInputUpserts {
		e := entity(input.ProjectionEpoch, fmt.Sprint(input.Cursor), input.Cursor, input.RevisionCursor)
		e.Entity = &historypb.ThreadEntity_ConfirmedInput{ConfirmedInput: &historypb.ThreadConfirmedInput{HarnessMessageId: input.HarnessMessageID, OriginCommandIds: slices.Clone(input.OriginCommandIDs), TurnId: input.TurnID, InputRef: ref(&input.Text)}}
		rows = append(rows, e)
	}
	for _, segment := range result.LifecycleUpserts {
		e := entity(segment.ProjectionEpoch, fmt.Sprint(segment.Cursor), segment.Cursor, segment.RevisionCursor)
		e.Entity = &historypb.ThreadEntity_Lifecycle{Lifecycle: &historypb.ThreadLifecycle{Event: segment.Event}}
		rows = append(rows, e)
	}
	for _, command := range result.CommandUpserts {
		revision := command.AdmissionCursor
		if command.OutcomeCursor != nil && *command.OutcomeCursor != 0 {
			revision = *command.OutcomeCursor
		}
		buildErr = errors.Join(buildErr, validateOutcome(command.Outcome))
		e := entity(command.ProjectionEpoch, command.CommandID, command.AdmissionCursor, revision)
		e.Entity = &historypb.ThreadEntity_Command{Command: &historypb.ThreadCommand{Operation: command.Operation, Outcome: command.Outcome, OutcomeCursor: command.OutcomeCursor, OutcomeReason: command.OutcomeReason, RequestedValue: command.RequestedValue, InputRef: ref(command.Input)}}
		rows = append(rows, e)
	}
	if buildErr != nil {
		return nil, buildErr
	}
	slices.SortFunc(rows, func(a, b *historypb.ThreadEntity) int {
		ka, _ := EntityKindOf(a)
		kb, _ := EntityKindOf(b)
		return cmp.Or(cmp.Compare(a.Cursor, b.Cursor), cmp.Compare(ka, kb), cmp.Compare(a.EntityId, b.EntityId))
	})
	return rows, nil
}
