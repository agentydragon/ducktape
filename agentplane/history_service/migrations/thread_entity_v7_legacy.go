// The v7 migration decodes the v6 storage format once to preserve existing history.
package migrations

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// threadEntityV6 is the immutable v6 persistence decoder. It deliberately belongs to this migration,
// rather than the runtime fold, which only reads canonical ProtoJSON after v7.
type threadEntityV6 struct {
	ThreadID        string          `json:"thread_id"`
	ProjectionEpoch string          `json:"projection_epoch"`
	EntityKind      string          `json:"entity_kind"`
	EntityID        string          `json:"entity_id"`
	Cursor          uint64          `json:"cursor"`
	RevisionCursor  uint64          `json:"revision_cursor"`
	Pending         bool            `json:"pending"`
	TurnID          *string         `json:"turn_id"`
	State           json.RawMessage `json:"state"`
	TextRef         json.RawMessage `json:"text_ref"`
	ArgumentsRef    json.RawMessage `json:"arguments_ref"`
	OutputRef       json.RawMessage `json:"output_ref"`
	InputRef        json.RawMessage `json:"input_ref"`
}

type legacyPayloadReference struct {
	ProjectionEpoch string `json:"projection_epoch"`
	OwnerCursor     uint64 `json:"owner_cursor,string"`
	OwnerID         string `json:"owner_id"`
	Field           string `json:"field"`
	RevisionCursor  uint64 `json:"revision_cursor,string"`
	Generation      uint64 `json:"generation,string"`
	ChunkCount      uint64 `json:"chunk_count,string"`
}

func decodeLegacy(data []byte, into any, required ...string) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	if fields == nil {
		return errors.New("expected a JSON object")
	}
	for _, field := range required {
		name := strings.TrimSuffix(field, "?")
		data, ok := fields[name]
		if !ok {
			return fmt.Errorf("missing field %s", name)
		}
		if !strings.HasSuffix(field, "?") && bytes.Equal(data, []byte("null")) {
			return fmt.Errorf("null field %s", name)
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(into); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return errors.New("trailing JSON data")
	}
	return nil
}

func (r threadEntityV6) reference(data json.RawMessage, field string, allowed bool) (*historypb.ThreadPayloadReference, error) {
	if len(data) == 0 || bytes.Equal(data, []byte("null")) {
		return nil, nil
	}
	if !allowed {
		return nil, fmt.Errorf("%s cannot have %s reference", r.EntityKind, field)
	}
	var ref legacyPayloadReference
	if err := decodeLegacy(data, &ref, "projection_epoch", "owner_cursor", "owner_id", "field", "revision_cursor", "generation", "chunk_count"); err != nil {
		return nil, err
	}
	if ref.Field != field || ref.ProjectionEpoch != r.ProjectionEpoch || ref.OwnerCursor != r.Cursor || ref.Generation < r.Cursor || ref.Generation > ref.RevisionCursor || ref.RevisionCursor > r.RevisionCursor {
		return nil, fmt.Errorf("invalid %s reference identity or revision", field)
	}
	if r.EntityKind != "confirmed_input" && ref.OwnerID != r.EntityID {
		return nil, errors.New("reference owner disagrees with entity")
	}
	if r.EntityKind != "item" && ref.RevisionCursor != r.Cursor {
		return nil, errors.New("input reference revision disagrees with entity")
	}
	var kind historypb.ThreadPayloadField
	switch field {
	case "text":
		kind = historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_TEXT
	case "arguments":
		kind = historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_ARGUMENTS
	case "output":
		kind = historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_OUTPUT
	case "confirmed_input":
		kind = historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_CONFIRMED_INPUT
	case "command_input":
		kind = historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_COMMAND_INPUT
	default:
		return nil, fmt.Errorf("unknown payload field %q", field)
	}
	return &historypb.ThreadPayloadReference{ProjectionEpoch: ref.ProjectionEpoch, OwnerCursor: ref.OwnerCursor, OwnerId: ref.OwnerID, Field: kind, RevisionCursor: ref.RevisionCursor, Generation: ref.Generation, ChunkCount: ref.ChunkCount}, nil
}

func (r threadEntityV6) canonicalState() (proto.Message, error) {
	if r.EntityKind != "command" && r.Pending {
		return nil, errors.New("non-command row is pending")
	}
	if r.EntityKind != "item" && r.EntityKind != "confirmed_input" && r.TurnID != nil {
		return nil, errors.New("unexpected turn_id")
	}
	text, err := r.reference(r.TextRef, "text", r.EntityKind == "item")
	if err != nil {
		return nil, err
	}
	arguments, err := r.reference(r.ArgumentsRef, "arguments", r.EntityKind == "item")
	if err != nil {
		return nil, err
	}
	output, err := r.reference(r.OutputRef, "output", r.EntityKind == "item")
	if err != nil {
		return nil, err
	}
	inputField := "confirmed_input"
	if r.EntityKind == "command" {
		inputField = "command_input"
	}
	input, err := r.reference(r.InputRef, inputField, r.EntityKind == "command" || r.EntityKind == "confirmed_input")
	if err != nil {
		return nil, err
	}
	switch r.EntityKind {
	case "item":
		var state struct {
			Kind           int32                        `json:"kind"`
			ToolName       string                       `json:"tool_name"`
			Completion     *string                      `json:"completion"`
			ToolSucceeded  *bool                        `json:"tool_succeeded"`
			Recovery       *eventpb.RecoveryDisposition `json:"recovery"`
			RecoveryReason *string                      `json:"recovery_reason"`
		}
		if err := decodeLegacy(r.State, &state, "kind", "tool_name", "completion?", "tool_succeeded?", "recovery?", "recovery_reason?"); err != nil {
			return nil, err
		}
		value := &historypb.ThreadItem{Kind: eventpb.ItemKind(state.Kind), ToolName: state.ToolName, TurnId: r.TurnID, Recovery: state.Recovery, RecoveryReason: state.RecoveryReason, TextRef: text, ArgumentsRef: arguments, OutputRef: output}
		switch {
		case state.Completion == nil && state.ToolSucceeded == nil:
		case state.Completion != nil && *state.Completion == "text" && state.ToolSucceeded == nil:
			value.Completion = &historypb.ThreadItem_Text{Text: &historypb.ThreadItemTextCompletion{}}
		case state.Completion != nil && *state.Completion == "tool" && state.ToolSucceeded != nil:
			value.Completion = &historypb.ThreadItem_Tool{Tool: &historypb.ThreadItemToolCompletion{Succeeded: *state.ToolSucceeded}}
		default:
			return nil, errors.New("invalid item completion")
		}
		return value, nil
	case "confirmed_input":
		var state struct {
			HarnessMessageID string   `json:"harness_message_id"`
			OriginCommandIDs []string `json:"origin_command_ids"`
		}
		if err := decodeLegacy(r.State, &state, "harness_message_id", "origin_command_ids?"); err != nil {
			return nil, err
		}
		if input != nil {
			ownerID := state.HarnessMessageID
			if ownerID == "" {
				ownerID = fmt.Sprintf("confirmed:%d", r.Cursor)
			}
			if input.OwnerId != ownerID {
				return nil, errors.New("confirmed input reference owner disagrees with state")
			}
		}
		return &historypb.ThreadConfirmedInput{HarnessMessageId: state.HarnessMessageID, OriginCommandIds: state.OriginCommandIDs, TurnId: r.TurnID, InputRef: input}, nil
	case "lifecycle":
		var state struct {
			Observation string          `json:"observation"`
			Event       json.RawMessage `json:"event"`
		}
		if err := decodeLegacy(r.State, &state, "observation", "event"); err != nil {
			return nil, err
		}
		event := &eventpb.Event{}
		if err := protojson.Unmarshal(state.Event, event); err != nil {
			return nil, err
		}
		observation := event.ProtoReflect().WhichOneof(event.ProtoReflect().Descriptor().Oneofs().ByName("observation"))
		if observation == nil || string(observation.Name()) != state.Observation {
			return nil, errors.New("lifecycle observation disagrees with Event")
		}
		return &historypb.ThreadLifecycle{Event: event}, nil
	case "command":
		var state struct {
			Operation      string  `json:"operation"`
			Outcome        string  `json:"outcome"`
			OutcomeCursor  *uint64 `json:"outcome_cursor,string"`
			OutcomeReason  *string `json:"outcome_reason"`
			RequestedValue *string `json:"requested_value"`
		}
		if err := decodeLegacy(r.State, &state, "operation", "outcome", "outcome_cursor?", "outcome_reason?"); err != nil {
			return nil, err
		}
		outcomes := map[string]historypb.ThreadCommandOutcome{"pending": historypb.ThreadCommandOutcome_THREAD_COMMAND_OUTCOME_PENDING, "effected": historypb.ThreadCommandOutcome_THREAD_COMMAND_OUTCOME_EFFECTED, "failed": historypb.ThreadCommandOutcome_THREAD_COMMAND_OUTCOME_FAILED, "noop": historypb.ThreadCommandOutcome_THREAD_COMMAND_OUTCOME_NOOP}
		outcome, ok := outcomes[state.Outcome]
		if !ok {
			return nil, fmt.Errorf("unknown command outcome %q", state.Outcome)
		}
		if r.Pending != (state.Outcome == "pending") {
			return nil, errors.New("pending disagrees with command outcome")
		}
		return &historypb.ThreadCommand{Operation: state.Operation, Outcome: outcome, OutcomeCursor: state.OutcomeCursor, OutcomeReason: state.OutcomeReason, RequestedValue: state.RequestedValue, InputRef: input}, nil
	case "view_state":
		var state struct {
			Controls    json.RawMessage `json:"controls"`
			Operational json.RawMessage `json:"operational"`
		}
		if err := decodeLegacy(r.State, &state, "controls", "operational"); err != nil {
			return nil, err
		}
		var controls struct {
			AppliedModel           *string `json:"applied_model"`
			AppliedReasoningEffort *string `json:"applied_reasoning_effort"`
			ActiveTurnID           *string `json:"active_turn_id"`
			HarnessState           *string `json:"harness_state"`
		}
		if err := decodeLegacy(state.Controls, &controls, "applied_model?", "active_turn_id?", "harness_state?"); err != nil {
			return nil, err
		}
		var operational struct {
			Status             string          `json:"status"`
			LastVerifiedCursor uint64          `json:"last_verified_cursor,string"`
			FeedError          json.RawMessage `json:"feed_error"`
		}
		if err := decodeLegacy(state.Operational, &operational, "status", "last_verified_cursor", "feed_error?"); err != nil {
			return nil, err
		}
		statuses := map[string]historypb.ThreadFeedStatus{"active": historypb.ThreadFeedStatus_THREAD_FEED_STATUS_ACTIVE, "ended": historypb.ThreadFeedStatus_THREAD_FEED_STATUS_ENDED, "failed": historypb.ThreadFeedStatus_THREAD_FEED_STATUS_FAILED}
		status, ok := statuses[operational.Status]
		if !ok {
			return nil, fmt.Errorf("unknown feed status %q", operational.Status)
		}
		var feedError *historypb.ThreadFeedError
		if !bytes.Equal(operational.FeedError, []byte("null")) {
			var failure struct {
				Cursor  *uint64 `json:"cursor,string"`
				Message string  `json:"message"`
			}
			if err := decodeLegacy(operational.FeedError, &failure, "cursor?", "message"); err != nil {
				return nil, err
			}
			feedError = &historypb.ThreadFeedError{Cursor: failure.Cursor, Message: failure.Message}
		}
		return &historypb.ThreadViewState{Controls: &historypb.ThreadControls{AppliedModel: controls.AppliedModel, AppliedReasoningEffort: controls.AppliedReasoningEffort, ActiveTurnId: controls.ActiveTurnID, HarnessState: controls.HarnessState}, Operational: &historypb.ThreadOperational{Status: status, LastVerifiedCursor: operational.LastVerifiedCursor, FeedError: feedError}}, nil
	default:
		return nil, fmt.Errorf("unknown entity kind %q", r.EntityKind)
	}
}
