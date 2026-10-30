// Package threadcommands registers the app's thread command HTTP endpoints.
package threadcommands

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"

	appbridge "github.com/agentydragon/ducktape/agentplane/app/go/bridge"
	"github.com/agentydragon/ducktape/agentplane/app/go/historydata"
	"github.com/agentydragon/ducktape/agentplane/app/go/httperror"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/danielgtaylor/huma/v2"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// ThreadStore is implemented by threads.Store.
type ThreadStore interface {
	GetThread(context.Context, uuid.UUID) (*threads.Thread, error)
}

// History is implemented by historydata.Store. AdmittedCommand validates the
// indexed admission and immutable event evidence; GetSummary supplies the
// Sandbox and attached harness for a new request.
type History interface {
	AdmittedCommand(context.Context, uuid.UUID, *commandpb.Command) (*eventlogpb.EventEntry, error)
	GetSummary(context.Context, uuid.UUID) (historydata.FoldSummary, error)
}

// CommandReadiness is implemented by bridge.RunnerBridge.
type CommandReadiness interface {
	CommandReady(context.Context, uuid.UUID) error
}

// Dispatcher is implemented by bridge.DispatcherClient.
type Dispatcher interface {
	SubmitInput(context.Context, *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error)
	ChangeModel(context.Context, *dispatcherpb.ChangeModelRequest) (*dispatcherpb.ChangeModelResponse, error)
	ChangeReasoningEffort(context.Context, *dispatcherpb.ChangeReasoningEffortRequest) (*dispatcherpb.ChangeReasoningEffortResponse, error)
	InterruptTurn(context.Context, *dispatcherpb.InterruptTurnRequest) (*dispatcherpb.InterruptTurnResponse, error)
}

// Dependencies bind the route handlers to app-owned Thread metadata and
// History-owned Sandbox/session content. Models is keyed by proto enum names,
// for example "HARNESS_CODEX".
type Dependencies struct {
	Threads    ThreadStore
	History    History
	Readiness  CommandReadiness
	Dispatcher Dispatcher
	Models     map[string][]string
}

// RegisterRoutes installs the four command routes on a caller-protected Huma
// API. Admission lookup happens before readiness and target resolution, so an
// exact retry can return its durable event even after Sandbox deletion.
func RegisterRoutes(api huma.API, dependencies Dependencies) error {
	if api == nil || dependencies.Threads == nil || dependencies.History == nil ||
		dependencies.Readiness == nil || dependencies.Dispatcher == nil {
		return errors.New("thread store, History store, readiness, dispatcher, and Huma API are required")
	}

	huma.Register[submitInputInput, commandSubmissionOutput](api, huma.Operation{
		OperationID: "submitThreadInput", Method: http.MethodPost, Path: "/threads/{thread_id}/submit-input", Tags: []string{"threads"},
		Summary: "Submit input to a Runner Session through the Session Dispatcher",
	}, func(ctx context.Context, input *submitInputInput) (*commandSubmissionOutput, error) {
		command := &commandpb.Command{CommandId: input.Body.CommandID, Operation: &commandpb.Command_SubmitInput{
			SubmitInput: &commandpb.SubmitInput{Text: input.Body.Text},
		}}
		thread, archived, err := submissionTarget(ctx, dependencies, input.ThreadID, command)
		if err != nil {
			return nil, commandHTTPError(err, input.Body.CommandID)
		}
		if archived != nil {
			return archivedOutput(archived)
		}
		response, err := dependencies.Dispatcher.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
			CommandId: input.Body.CommandID, SessionId: input.ThreadID.String(), Sandbox: thread.Sandbox,
			SandboxUid: thread.SandboxUID, Text: input.Body.Text,
		})
		if err != nil {
			return nil, commandHTTPError(err, input.Body.CommandID)
		}
		encoded, err := protoJSONMap(response.GetDispatch())
		if err != nil {
			return nil, huma.Error500InternalServerError("Dispatcher result could not be encoded")
		}
		return &commandSubmissionOutput{Body: commandSubmissionBody{Kind: "dispatch", Dispatch: encoded}}, nil
	})

	huma.Register[changeModelInput, commandSubmissionOutput](api, huma.Operation{
		OperationID: "changeThreadModel", Method: http.MethodPost, Path: "/threads/{thread_id}/change-model", Tags: []string{"threads"},
		Summary: "Change the model for a Runner Session",
	}, func(ctx context.Context, input *changeModelInput) (*commandSubmissionOutput, error) {
		command := &commandpb.Command{CommandId: input.Body.CommandID, Operation: &commandpb.Command_ChangeModel{
			ChangeModel: &commandpb.ChangeModel{Model: input.Body.Model},
		}}
		thread, archived, err := submissionTarget(ctx, dependencies, input.ThreadID, command)
		if err != nil {
			return nil, commandHTTPError(err, input.Body.CommandID)
		}
		if archived != nil {
			return archivedOutput(archived)
		}
		if !supportsModel(dependencies.Models, thread, input.Body.Model) {
			return nil, huma.Error422UnprocessableEntity("model is incompatible with this thread's harness")
		}
		response, err := dependencies.Dispatcher.ChangeModel(ctx, &dispatcherpb.ChangeModelRequest{
			CommandId: input.Body.CommandID, SessionId: input.ThreadID.String(), Sandbox: thread.Sandbox,
			SandboxUid: thread.SandboxUID, Model: input.Body.Model,
		})
		if err != nil {
			return nil, commandHTTPError(err, input.Body.CommandID)
		}
		encoded, err := protoJSONMap(response.GetDispatch())
		if err != nil {
			return nil, huma.Error500InternalServerError("Dispatcher result could not be encoded")
		}
		return &commandSubmissionOutput{Body: commandSubmissionBody{Kind: "dispatch", Dispatch: encoded}}, nil
	})

	huma.Register[changeReasoningEffortInput, commandSubmissionOutput](api, huma.Operation{
		OperationID: "changeThreadReasoningEffort", Method: http.MethodPost, Path: "/threads/{thread_id}/change-reasoning-effort", Tags: []string{"threads"},
		Summary: "Change the reasoning effort for a Runner Session",
	}, func(ctx context.Context, input *changeReasoningEffortInput) (*commandSubmissionOutput, error) {
		command := &commandpb.Command{CommandId: input.Body.CommandID, Operation: &commandpb.Command_ChangeReasoningEffort{
			ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: input.Body.Effort},
		}}
		thread, archived, err := submissionTarget(ctx, dependencies, input.ThreadID, command)
		if err != nil {
			return nil, commandHTTPError(err, input.Body.CommandID)
		}
		if archived != nil {
			return archivedOutput(archived)
		}
		response, err := dependencies.Dispatcher.ChangeReasoningEffort(ctx, &dispatcherpb.ChangeReasoningEffortRequest{
			CommandId: input.Body.CommandID, SessionId: input.ThreadID.String(), Sandbox: thread.Sandbox,
			SandboxUid: thread.SandboxUID, Effort: input.Body.Effort,
		})
		if err != nil {
			return nil, commandHTTPError(err, input.Body.CommandID)
		}
		encoded, err := protoJSONMap(response.GetDispatch())
		if err != nil {
			return nil, huma.Error500InternalServerError("Dispatcher result could not be encoded")
		}
		return &commandSubmissionOutput{Body: commandSubmissionBody{Kind: "dispatch", Dispatch: encoded}}, nil
	})

	huma.Register[interruptTurnInput, interruptSubmissionOutput](api, huma.Operation{
		OperationID: "interruptThreadTurn", Method: http.MethodPost, Path: "/threads/{thread_id}/interrupt-turn", Tags: []string{"threads"},
		Summary: "Interrupt the specified active Runner turn",
	}, func(ctx context.Context, input *interruptTurnInput) (*interruptSubmissionOutput, error) {
		command := &commandpb.Command{CommandId: input.Body.CommandID, Operation: &commandpb.Command_InterruptTurn{
			InterruptTurn: &commandpb.InterruptTurn{TurnId: input.Body.TurnID},
		}}
		thread, archived, err := submissionTarget(ctx, dependencies, input.ThreadID, command)
		if err != nil {
			return nil, commandHTTPError(err, input.Body.CommandID)
		}
		if archived != nil {
			encoded, encodeErr := protoJSONMap(archived)
			if encodeErr != nil {
				return nil, huma.Error500InternalServerError("Archived admission could not be encoded")
			}
			return &interruptSubmissionOutput{Body: interruptSubmissionBody{Kind: "archived_admission", Admission: encoded}}, nil
		}
		response, err := dependencies.Dispatcher.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{
			CommandId: input.Body.CommandID, SessionId: input.ThreadID.String(), Sandbox: thread.Sandbox,
			SandboxUid: thread.SandboxUID, TurnId: input.Body.TurnID,
		})
		if err != nil {
			return nil, commandHTTPError(err, input.Body.CommandID)
		}
		encoded, err := protoJSONMap(response)
		if err != nil {
			return nil, huma.Error500InternalServerError("Interrupt result could not be encoded")
		}
		return &interruptSubmissionOutput{Body: interruptSubmissionBody{Kind: "interrupt_result", Result: encoded}}, nil
	})
	return nil
}

func submissionTarget(ctx context.Context, dependencies Dependencies, id uuid.UUID, command *commandpb.Command) (historydata.FoldSummary, *eventlogpb.EventEntry, error) {
	// Python checks History's archived command evidence before app metadata or
	// live routing. Preserve that ordering so exact retries remain durable.
	admission, err := dependencies.History.AdmittedCommand(ctx, id, command)
	if err != nil {
		return historydata.FoldSummary{}, nil, err
	}
	if admission != nil {
		return historydata.FoldSummary{}, admission, nil
	}
	thread, err := dependencies.Threads.GetThread(ctx, id)
	if err != nil {
		return historydata.FoldSummary{}, nil, err
	}
	if thread == nil {
		return historydata.FoldSummary{}, nil, &appbridge.ThreadNotFoundError{ThreadID: id}
	}
	summary, err := dependencies.History.GetSummary(ctx, id)
	if err != nil {
		return historydata.FoldSummary{}, nil, err
	}
	if !summary.Present {
		return historydata.FoldSummary{}, nil, &appbridge.ThreadNotFoundError{ThreadID: id}
	}
	if summary.Sandbox == "" {
		return historydata.FoldSummary{}, nil, errors.New("History Session has no Sandbox target")
	}
	if summary.SandboxUID == "" {
		return historydata.FoldSummary{}, nil, &appbridge.ThreadStateError{Message: "History Session has no Sandbox identity"}
	}
	if err := dependencies.Readiness.CommandReady(ctx, id); err != nil {
		return historydata.FoldSummary{}, nil, err
	}
	return summary, nil, nil
}

func supportsModel(models map[string][]string, summary historydata.FoldSummary, model string) bool {
	harness := ""
	if summary.Feed != nil && summary.Feed.Attached != nil && summary.Feed.Attached.GetSpec() != nil {
		harness = runnerpb.Harness_name[int32(summary.Feed.Attached.GetSpec().GetHarness())]
	}
	if harness == "" || model == "" {
		return false
	}
	for _, offered := range models[harness] {
		if offered == model {
			return true
		}
	}
	return false
}

func archivedOutput(admission *eventlogpb.EventEntry) (*commandSubmissionOutput, error) {
	encoded, err := protoJSONMap(admission)
	if err != nil {
		return nil, huma.Error500InternalServerError("Archived admission could not be encoded")
	}
	return &commandSubmissionOutput{Body: commandSubmissionBody{Kind: "archived_admission", Admission: encoded}}, nil
}

func protoJSONMap(message proto.Message) (map[string]any, error) {
	encoded, err := protojson.Marshal(message)
	if err != nil {
		return nil, err
	}
	var result map[string]any
	if err := json.Unmarshal(encoded, &result); err != nil {
		return nil, err
	}
	return result, nil
}

func commandHTTPError(err error, commandID string) error {
	return httperror.LogServerError(classifyCommandError(err, commandID), err)
}

func classifyCommandError(err error, commandID string) error {
	var timeout *appbridge.DispatcherRequestTimeoutError
	var requestTimeout *appbridge.RequestTimeoutError
	var threadMissing *appbridge.ThreadNotFoundError
	var threadState *appbridge.ThreadStateError
	var refused *appbridge.RefusedError
	var service *appbridge.ServiceError
	switch {
	case errors.As(err, &timeout):
		return huma.Error504GatewayTimeout(err.Error())
	case errors.As(err, &requestTimeout):
		return huma.Error504GatewayTimeout((&appbridge.DispatcherRequestTimeoutError{CommandID: commandID, Cause: err}).Error())
	case errors.As(err, &threadMissing), errors.Is(err, threads.ErrNotFound), errors.Is(err, historydata.ErrNotFound):
		return huma.Error404NotFound(err.Error())
	case errors.As(err, &threadState), errors.As(err, &refused), errors.Is(err, historydata.ErrCommandConflict):
		return huma.Error409Conflict(err.Error())
	case errors.As(err, &service):
		switch service.Code {
		case codes.NotFound:
			return huma.Error404NotFound(err.Error())
		case codes.FailedPrecondition, codes.AlreadyExists, codes.Aborted:
			return huma.Error409Conflict(err.Error())
		case codes.InvalidArgument:
			return huma.Error422UnprocessableEntity(err.Error())
		case codes.ResourceExhausted:
			return huma.Error429TooManyRequests(err.Error())
		default:
			return huma.Error503ServiceUnavailable("Upstream service unavailable")
		}
	case errors.Is(err, context.DeadlineExceeded):
		return huma.Error504GatewayTimeout("Upstream deadline expired")
	default:
		return huma.Error503ServiceUnavailable("Upstream service unavailable")
	}
}

// ThreadPathInput is exported so Huma's reflection sees and documents the embedded path parameter.
type ThreadPathInput struct {
	ThreadID uuid.UUID `path:"thread_id" format:"uuid"`
}

type submitInputInput struct {
	ThreadPathInput
	Body submitInputBody
}

type submitInputBody struct {
	CommandID string `json:"commandId" minLength:"1"`
	Text      string `json:"text"`
}

func (b *submitInputBody) UnmarshalJSON(data []byte) error {
	type alias submitInputBody
	return unmarshalStrict(data, (*alias)(b))
}

type changeModelInput struct {
	ThreadPathInput
	Body changeModelBody
}

type changeModelBody struct {
	CommandID string `json:"commandId" minLength:"1"`
	Model     string `json:"model"`
}

func (b *changeModelBody) UnmarshalJSON(data []byte) error {
	type alias changeModelBody
	return unmarshalStrict(data, (*alias)(b))
}

type changeReasoningEffortInput struct {
	ThreadPathInput
	Body changeReasoningEffortBody
}

type changeReasoningEffortBody struct {
	CommandID string `json:"commandId" minLength:"1"`
	Effort    string `json:"effort"`
}

func (b *changeReasoningEffortBody) UnmarshalJSON(data []byte) error {
	type alias changeReasoningEffortBody
	return unmarshalStrict(data, (*alias)(b))
}

type interruptTurnInput struct {
	ThreadPathInput
	Body interruptTurnBody
}

type interruptTurnBody struct {
	CommandID string `json:"commandId" minLength:"1"`
	TurnID    string `json:"turnId"`
}

func (b *interruptTurnBody) UnmarshalJSON(data []byte) error {
	type alias interruptTurnBody
	return unmarshalStrict(data, (*alias)(b))
}

func unmarshalStrict(data []byte, destination any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		if err == nil {
			return errors.New("request body contains more than one JSON value")
		}
		return err
	}
	return nil
}

type commandSubmissionOutput struct{ Body commandSubmissionBody }

type commandSubmissionBody struct {
	Kind      string         `json:"kind"`
	Dispatch  map[string]any `json:"dispatch,omitempty"`
	Admission map[string]any `json:"admission,omitempty"`
}

type interruptSubmissionOutput struct{ Body interruptSubmissionBody }

type interruptSubmissionBody struct {
	Kind      string         `json:"kind"`
	Result    map[string]any `json:"result,omitempty"`
	Admission map[string]any `json:"admission,omitempty"`
}
