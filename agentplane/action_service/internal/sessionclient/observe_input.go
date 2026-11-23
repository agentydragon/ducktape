// Package sessionclient contains caller-neutral projections over the Session Dispatcher and
// History Service APIs. It owns no credentials and makes no authorization decisions.
package sessionclient

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/dispatchstate"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const maxObserveWaitSeconds = 30

type ObservationKind string

const (
	ObservationNotObserved    ObservationKind = "not_observed"
	ObservationInputConfirmed ObservationKind = "input_confirmed"
	ObservationTurnComplete   ObservationKind = "turn_completed"
	ObservationCommandFailed  ObservationKind = "command_failed"
	ObservationCommandNoop    ObservationKind = "command_no_op"
)

// ObserveInputRequest names a durable command and bounds how long this read may wait. ResumeToken
// is only a cursor hint; the call still authorizes both services and rebuilds the observation from
// the command's retained admission cursor.
type ObserveInputRequest struct {
	SessionID   string
	CommandID   string
	WaitSeconds int
	ResumeToken string
}

// SourceError is a fixed public error description. Backend messages and credentials are not copied.
type SourceError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// NewSourceError describes a failed backend read by its gRPC code alone. An error without a gRPC
// status, or with code Unknown, is reported as Unavailable.
func NewSourceError(err error) *SourceError {
	code := status.Code(err)
	if code == codes.Unknown {
		code = codes.Unavailable
	}
	message := "service unavailable"
	switch code {
	case codes.Canceled:
		message = "read canceled"
	case codes.DeadlineExceeded:
		message = "read deadline expired"
	case codes.NotFound:
		message = "not found"
	case codes.PermissionDenied, codes.Unauthenticated:
		message = "permission denied"
	case codes.InvalidArgument:
		message = "invalid request"
	case codes.DataLoss:
		message = "retained history has a gap"
	case codes.OutOfRange:
		message = "dispatch deletion coverage expired; resync required"
	}
	return &SourceError{Code: code.String(), Message: message}
}

type CorrelatedTurn struct {
	TurnID                string   `json:"turn_id"`
	InputCommandIDs       []string `json:"input_command_ids"`
	Status                string   `json:"status"`
	Error                 *string  `json:"error,omitempty"`
	FinalAssistantMessage *string  `json:"final_assistant_message,omitempty"`
}

type InputResult struct {
	HistoryCoverage
	Kind   ObservationKind `json:"kind"`
	TurnID *string         `json:"turn_id,omitempty"`
	Turn   *CorrelatedTurn `json:"turn,omitempty"`
	Reason *string         `json:"reason,omitempty"`
}

type InputObservation struct {
	SessionID   string                             `json:"session_id"`
	CommandID   string                             `json:"command_id"`
	Dispatch    SourcePart[*dispatcherpb.Dispatch] `json:"dispatch"`
	Observation InputResult                        `json:"observation"`
	resume      resumeToken
}

type resumeToken struct {
	Version       int    `json:"v"`
	SessionID     string `json:"session_id"`
	CommandID     string `json:"command_id"`
	StartCursor   uint64 `json:"start_cursor"`
	Through       uint64 `json:"through_cursor"`
	DispatchToken string `json:"dispatch_token,omitempty"`
}

// ObserveInput resolves the caller-held command ID before reading History, then follows only the
// events needed to associate that command with its turn. It never submits, retries or cancels work.
func ObserveInput(
	ctx context.Context,
	dispatcher dispatcherpb.SessionDispatcherClient,
	history historypb.HistoryServiceClient,
	request ObserveInputRequest,
) (result InputObservation, err error) {
	if _, err := uuid.Parse(request.SessionID); err != nil {
		return InputObservation{}, status.Error(codes.InvalidArgument, "session_id must be a UUID")
	}
	if _, err := uuid.Parse(request.CommandID); err != nil {
		return InputObservation{}, status.Error(codes.InvalidArgument, "command_id must be a UUID")
	}
	if request.WaitSeconds < 0 || request.WaitSeconds > maxObserveWaitSeconds {
		return InputObservation{}, status.Error(codes.InvalidArgument, "wait_seconds must be between 0 and 30")
	}
	position := resumeToken{Version: 1, SessionID: request.SessionID, CommandID: request.CommandID}
	if request.ResumeToken != "" {
		position, err = decodeResumeToken(request.ResumeToken, request.SessionID, request.CommandID)
		if err != nil {
			return InputObservation{}, status.Error(codes.InvalidArgument, "invalid resume token for this Session and command")
		}
	}

	result = InputObservation{
		resume:    position,
		SessionID: request.SessionID,
		CommandID: request.CommandID,
		Dispatch:  SourcePart[*dispatcherpb.Dispatch]{Kind: SourceNotObserved, Source: SourceDispatcher},
		Observation: InputResult{
			HistoryCoverage: HistoryCoverage{Source: SourceHistory, Gaps: []HistoryCursorRange{}},
			Kind:            ObservationNotObserved,
		},
	}
	defer func() { result.Observation.ResumeToken = encodeResumeToken(result.resume) }()
	readCtx := ctx
	var cancel context.CancelFunc
	if request.WaitSeconds > 0 {
		readCtx, cancel = context.WithTimeout(ctx, time.Duration(request.WaitSeconds)*time.Second)
		defer cancel()
	}

	dispatch, err := getDispatch(readCtx, dispatcher, request.CommandID, request.SessionID, &result)
	if err != nil && status.Code(err) != codes.NotFound {
		return result, err
	}
	if dispatch != nil && dispatch.GetOperation() != commandpb.Operation_OPERATION_SUBMIT_INPUT {
		return result, status.Error(codes.InvalidArgument, "command_id does not identify submitted input")
	}
	if dispatch == nil || (dispatch.GetAdmission() == nil && !dispatchTerminal(dispatch)) {
		dispatch, err = watchForAdmission(readCtx, dispatcher, request, dispatch, &result)
		if err != nil {
			if isWaitDeadline(readCtx, ctx) {
				return result, nil
			}
			if errors.Is(ctx.Err(), context.Canceled) {
				return result, status.FromContextError(ctx.Err()).Err()
			}
			result.Dispatch.Kind = SourcePartial
			if result.Dispatch.Error == nil {
				result.Dispatch.Error = NewSourceError(err)
			}
			return result, nil
		}
	}
	if dispatch == nil {
		if result.Dispatch.Error != nil {
			return result, nil
		}
		return result, status.Error(codes.NotFound, "input not found")
	}
	if dispatch.GetOperation() != commandpb.Operation_OPERATION_SUBMIT_INPUT {
		return result, status.Error(codes.InvalidArgument, "command_id does not identify submitted input")
	}
	startCursor, hasAdmission := admissionStart(dispatch)
	if !hasAdmission {
		if dispatchstate.NormalizeDeliveryState(dispatch.GetState()) == dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED {
			result.Dispatch.Kind = SourcePartial
			result.Dispatch.Error = &SourceError{Code: codes.DataLoss.String(), Message: "dispatcher admission cursor is missing"}
		}
		return result, nil
	}

	// History correlation is rebuilt from admission; the Dispatch cursor is independent.
	result.resume.StartCursor = startCursor
	result.resume.Through = 0
	return followInput(readCtx, ctx, history, request, startCursor, &result)
}

func getDispatch(
	ctx context.Context,
	client dispatcherpb.SessionDispatcherClient,
	commandID, sessionID string,
	result *InputObservation,
) (*dispatcherpb.Dispatch, error) {
	response, err := client.GetDispatch(ctx, &dispatcherpb.GetDispatchRequest{CommandId: commandID})
	if err != nil {
		return nil, err
	}
	dispatch := response.GetDispatch()
	if dispatch == nil || dispatch.GetCommandId() != commandID || dispatch.GetSessionId() != sessionID {
		return nil, status.Error(codes.NotFound, "input not found")
	}
	recordDispatch(dispatch, result)
	return dispatch, nil
}

func watchForAdmission(
	ctx context.Context,
	client dispatcherpb.SessionDispatcherClient,
	request ObserveInputRequest,
	current *dispatcherpb.Dispatch,
	result *InputObservation,
) (*dispatcherpb.Dispatch, error) {
	for {
		if err := ctx.Err(); err != nil {
			return current, status.FromContextError(err).Err()
		}
		token := result.resume.DispatchToken
		streamCtx, cancel := context.WithCancel(ctx)
		stream, err := client.WatchDispatches(streamCtx, &dispatcherpb.WatchDispatchesRequest{
			Filter: &dispatcherpb.DispatchFilter{
				Scope:      &dispatcherpb.DispatchFilter_SessionId{SessionId: request.SessionID},
				CommandIds: []string{request.CommandID},
			},
			ResumeToken: token,
		})
		if err == nil {
			for {
				var frame *dispatcherpb.WatchDispatchesResponse
				frame, err = stream.Recv()
				if err != nil {
					break
				}
				if dispatch := frame.GetDispatch(); dispatch != nil {
					if dispatch.GetCommandId() != request.CommandID || dispatch.GetSessionId() != request.SessionID {
						cancel()
						return nil, status.Error(codes.DataLoss, "dispatcher returned a different input")
					}
					recordDispatch(dispatch, result)
					current = dispatch
				}
				// Advance only after the receipt is applied. Persist CaughtUp tokens too.
				result.resume.DispatchToken = frame.GetResumeToken()
				if current != nil && (current.GetAdmission() != nil || dispatchTerminal(current) || wakeBlocksDelivery(current)) {
					cancel()
					return current, nil
				}
				if frame.GetCaughtUp() != nil {
					if current == nil && token != "" {
						// The caller has a position but no receipt. Restore it from a snapshot
						// even if its terminal frame was already covered by the cursor.
						result.resume.DispatchToken = ""
						break
					}
					if current == nil || request.WaitSeconds == 0 {
						cancel()
						return current, nil
					}
				}
			}
		}
		cancel()
		if status.Code(err) == codes.OutOfRange && result.resume.DispatchToken != "" {
			result.resume.DispatchToken = ""
			result.Dispatch = SourcePart[*dispatcherpb.Dispatch]{
				Kind: SourcePartial, Source: SourceDispatcher,
				Error: &SourceError{Code: codes.OutOfRange.String(), Message: "dispatch deletion coverage expired; resyncing from a fresh snapshot"},
			}
			current = nil
			continue
		}
		if err != nil {
			return current, err
		}
	}
}

func wakeBlocksDelivery(dispatch *dispatcherpb.Dispatch) bool {
	switch dispatch.GetWakeAssessment().GetState() {
	case environmentlifecyclepb.WakeState_WAKE_STATE_AUTOMATIC_WAKE_DISABLED,
		environmentlifecyclepb.WakeState_WAKE_STATE_DENIED,
		environmentlifecyclepb.WakeState_WAKE_STATE_RATE_LIMITED,
		environmentlifecyclepb.WakeState_WAKE_STATE_FAILED,
		environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN:
		return true
	default:
		return false
	}
}

func recordDispatch(dispatch *dispatcherpb.Dispatch, result *InputObservation) {
	dispatch.State = dispatchstate.NormalizeDeliveryState(dispatch.GetState())
	now := time.Now().UTC()
	coverageError := result.Dispatch.Error
	result.Dispatch = SourcePart[*dispatcherpb.Dispatch]{Kind: SourceObserved, Source: SourceDispatcher, ObservedAt: &now, Value: dispatch}
	if coverageError != nil && coverageError.Code == codes.OutOfRange.String() {
		result.Dispatch.Kind = SourcePartial
		result.Dispatch.Error = coverageError
	}
}

func setObservation(result *InputObservation, kind ObservationKind, turnID *string, turn *CorrelatedTurn, reason *string) {
	coverage := result.Observation.HistoryCoverage
	result.Observation = InputResult{
		HistoryCoverage: coverage,
		Kind:            kind,
		TurnID:          turnID,
		Turn:            turn,
		Reason:          reason,
	}
}

func dispatchTerminal(dispatch *dispatcherpb.Dispatch) bool {
	switch dispatchstate.NormalizeDeliveryState(dispatch.GetState()) {
	case dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED,
		dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER,
		dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN:
		return true
	default:
		return false
	}
}

func admissionStart(dispatch *dispatcherpb.Dispatch) (uint64, bool) {
	if admission := dispatch.GetAdmission(); admission != nil && admission.GetCursor() > 0 {
		return admission.GetCursor() - 1, true
	}
	return 0, false
}

func followInput(
	ctx, parent context.Context,
	client historypb.HistoryServiceClient,
	request ObserveInputRequest,
	startCursor uint64,
	result *InputObservation,
) (InputObservation, error) {
	through := startCursor
	activeTurnID := ""
	var correlated *CorrelatedTurn
	for {
		stream, err := client.FollowSession(ctx, &historypb.FollowSessionRequest{
			SessionId: request.SessionID,
			Start:     &historypb.FollowSessionRequest_AfterCursor{AfterCursor: through},
			Kinds: []historypb.EventKind{
				historypb.EventKind_EVENT_KIND_COMMAND_FAILED,
				historypb.EventKind_EVENT_KIND_COMMAND_NOOP,
				historypb.EventKind_EVENT_KIND_HARNESS_USER_MESSAGE_CONFIRMED,
				historypb.EventKind_EVENT_KIND_TURN_STARTED,
				historypb.EventKind_EVENT_KIND_TURN_COMPLETED,
				historypb.EventKind_EVENT_KIND_ITEM_COMPLETED,
			},
			Follow: request.WaitSeconds > 0,
		})
		if err != nil {
			return historyFailure(err, ctx, parent, result)
		}
		renew := false
		for {
			frame, err := stream.Recv()
			if err != nil {
				if isWaitDeadline(ctx, parent) {
					return *result, nil
				}
				return historyFailure(err, ctx, parent, result)
			}
			if _, corrected := frame.GetFrame().(*historypb.FollowSessionResponse_Removed); corrected {
				removed := frame.GetRemoved()
				result.Observation.Gaps = append(result.Observation.Gaps, HistoryCursorRange{First: removed.GetFirst(), Last: removed.GetLast()})
				result.Observation.Error = &SourceError{Code: codes.DataLoss.String(), Message: "History contains a removed cursor range"}
				if correlated == nil {
					setObservation(result, ObservationNotObserved, nil, nil, nil)
				}
				return *result, nil
			}
			var complete bool
			switch entryFrame := frame.GetFrame().(type) {
			case *historypb.FollowSessionResponse_Entries:
				for _, entry := range entryFrame.Entries.GetEntries() {
					event := entry.GetEvent()
					switch observation := event.GetObservation().(type) {
					case *eventpb.Event_CommandFailed:
						if observation.CommandFailed.GetCommandId() == request.CommandID {
							setObservation(result, ObservationCommandFailed, nil, nil, new(observation.CommandFailed.GetReason()))
							complete = true
						}
					case *eventpb.Event_CommandNoop:
						if observation.CommandNoop.GetCommandId() == request.CommandID {
							setObservation(result, ObservationCommandNoop, nil, nil, new(observation.CommandNoop.GetReason()))
							complete = true
						}
					case *eventpb.Event_HarnessUserMessageConfirmed:
						confirmed := observation.HarnessUserMessageConfirmed
						if slices.Contains(confirmed.GetOriginCommandIds(), request.CommandID) {
							correlated = &CorrelatedTurn{TurnID: confirmed.GetTurnId(), InputCommandIDs: slices.Clone(confirmed.GetOriginCommandIds())}
							setObservation(result, ObservationInputConfirmed, &correlated.TurnID, nil, nil)
						}
					case *eventpb.Event_TurnStarted:
						activeTurnID = observation.TurnStarted.GetTurnId()
					case *eventpb.Event_ItemCompleted:
						if text, ok := observation.ItemCompleted.GetOutcome().(*eventpb.ItemCompleted_Text); ok && correlated != nil && activeTurnID == correlated.TurnID {
							correlated.FinalAssistantMessage = new(text.Text)
						}
					case *eventpb.Event_TurnCompleted:
						completedTurn := observation.TurnCompleted
						if correlated != nil && completedTurn.GetTurnId() == correlated.TurnID {
							correlated.Status = completedTurn.GetStatus().String()
							if completedTurn.GetError() != "" {
								correlated.Error = new(completedTurn.GetError())
							}
							setObservation(result, ObservationTurnComplete, nil, correlated, nil)
							complete = true
						} else {
							activeTurnID = ""
						}
					}
					if complete {
						break
					}
				}
			case *historypb.FollowSessionResponse_CaughtUp:
				if request.WaitSeconds == 0 {
					complete = true
				}
			case *historypb.FollowSessionResponse_ReconnectRequired:
				renew = true
			}
			if frame.GetThroughCursor() > through {
				through = frame.GetThroughCursor()
			}
			if through > 0 {
				setCoverage(result, through, startCursor)
			}
			if complete {
				return *result, nil
			}
			if renew {
				break
			}
		}
		if !renew {
			return *result, nil
		}
	}
}

func setCoverage(result *InputObservation, through, start uint64) {
	result.Observation.ThroughCursor = fmt.Sprint(through)
	result.resume.StartCursor = start
	result.resume.Through = through
}

func historyFailure(err error, ctx, parent context.Context, result *InputObservation) (InputObservation, error) {
	if isWaitDeadline(ctx, parent) {
		return *result, nil
	}
	if errors.Is(parent.Err(), context.Canceled) {
		return *result, status.FromContextError(parent.Err()).Err()
	}
	result.Observation.Error = NewSourceError(err)
	return *result, nil
}

func isWaitDeadline(ctx, parent context.Context) bool {
	return errors.Is(ctx.Err(), context.DeadlineExceeded) && parent.Err() == nil
}

func encodeResumeToken(token resumeToken) string {
	encoded, err := json.Marshal(token)
	if err != nil {
		panic(err)
	}
	return base64.RawURLEncoding.EncodeToString(encoded)
}

func decodeResumeToken(token, sessionID, commandID string) (resumeToken, error) {
	encoded, err := base64.RawURLEncoding.DecodeString(token)
	var decoded resumeToken
	if err != nil || json.Unmarshal(encoded, &decoded) != nil || decoded.Version != 1 ||
		decoded.SessionID != sessionID || decoded.CommandID != commandID {
		return resumeToken{}, errors.New("invalid observation resume token")
	}
	return decoded, nil
}
