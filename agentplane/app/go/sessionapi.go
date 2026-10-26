package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	appbridge "github.com/agentydragon/ducktape/agentplane/app/go/bridge"
	"github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/observability"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/danielgtaylor/huma/v2"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
)

type sessionOpener interface {
	OpenSession(context.Context, string, uuid.UUID, *runnerpb.SessionSpec, *string) (*dispatcherpb.SessionOpening, error)
	SessionOpening(context.Context, string, uuid.UUID) (*dispatcherpb.SessionOpening, error)
}

type threadOpener interface {
	OpenThread(context.Context, uuid.UUID, bool) error
}

type newSessionInput struct {
	Name string `path:"name" minLength:"1" maxLength:"57" pattern:"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"`
	Body newSessionBody
}

type newSessionBody struct {
	SessionID               uuid.UUID      `json:"session_id" format:"uuid"`
	Spec                    map[string]any `json:"spec"`
	AutoTitleOnFirstMessage *bool          `json:"auto_title_on_first_message,omitempty"`
	SetupScript             *string        `json:"setup_script,omitempty"`
	SetupScriptPresent      bool           `json:"-"`
}

func (b *newSessionBody) UnmarshalJSON(data []byte) error {
	type bodyAlias newSessionBody
	var body bodyAlias
	if err := json.Unmarshal(data, &body); err != nil {
		return err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	*b = newSessionBody(body)
	_, b.SetupScriptPresent = fields["setup_script"]
	return nil
}

type sessionOpeningOutput struct{ Body map[string]any }

// RegisterSessionRoutes preserves the caller-minted idempotency key, configured Sandbox preset
// defaults, and durable thread metadata before dispatching the generated Session Dispatcher RPC.
func RegisterSessionRoutes(
	server *HTTPServer,
	config Config,
	inventory *sandboxes.Access,
	bridge sessionOpener,
	store threadOpener,
	titlesAvailable bool,
) {
	api := server.ProtectedAPI()
	huma.Register[newSessionInput, sessionOpeningOutput](api, huma.Operation{
		OperationID: "openSandboxSession", Method: http.MethodPost, Path: "/sandboxes/{name}/sessions", Tags: []string{"sessions"},
		Summary: "Open a durable Runner Session through the Session Dispatcher", DefaultStatus: http.StatusCreated,
	}, func(ctx context.Context, input *newSessionInput) (_ *sessionOpeningOutput, routeErr error) {
		ctx, span := observability.Start(ctx, "agentplane.session.create")
		defer func() { observability.End(span, routeErr) }()
		body := input.Body
		if body.Spec == nil {
			return nil, huma.Error422UnprocessableEntity("spec is required")
		}
		sandbox, err := inventory.Get(ctx, input.Name)
		if err != nil {
			return nil, sandboxHTTPError(err)
		}
		defaults, setupScript, err := sessionDefaults(config, sandbox, body.SetupScriptPresent, body.SetupScript)
		if err != nil {
			return nil, huma.Error409Conflict(err.Error())
		}
		spec, err := appbridge.ParseSessionSpec(defaults, body.Spec)
		if err != nil {
			return nil, huma.Error422UnprocessableEntity(err.Error())
		}
		spec.Cwd = replaceSessionID(spec.GetCwd(), body.SessionID.String())
		autoTitle := config.AutoTitleOnFirstMessageDefault
		if body.AutoTitleOnFirstMessage != nil {
			autoTitle = *body.AutoTitleOnFirstMessage
		}
		if autoTitle && !titlesAvailable {
			return nil, huma.Error409Conflict("automatic Thread titles are not configured")
		}
		if err := store.OpenThread(ctx, body.SessionID, autoTitle); err != nil {
			return nil, huma.Error503ServiceUnavailable("Thread metadata is unavailable")
		}
		opening, err := bridge.OpenSession(ctx, input.Name, body.SessionID, spec, setupScript)
		if err != nil {
			return nil, sessionHTTPError(err)
		}
		return openingOutput(opening)
	})
	huma.Register[sessionOpeningInput, sessionOpeningOutput](api, huma.Operation{
		OperationID: "getSandboxSessionOpening", Method: http.MethodGet, Path: "/sandboxes/{name}/sessions/{session_id}/opening", Tags: []string{"sessions"},
		Summary: "Read the durable opening response for a Session",
	}, func(ctx context.Context, input *sessionOpeningInput) (*sessionOpeningOutput, error) {
		opening, err := bridge.SessionOpening(ctx, input.Name, input.SessionID)
		if err != nil {
			return nil, sessionHTTPError(err)
		}
		return openingOutput(opening)
	})
}

type sessionOpeningInput struct {
	Name      string    `path:"name" minLength:"1" maxLength:"57" pattern:"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"`
	SessionID uuid.UUID `path:"session_id" format:"uuid"`
}

func sessionDefaults(config Config, sandbox *sandboxespb.Sandbox, setupWasSet bool, requestedSetup *string) (*runnerpb.SessionSpec, *string, error) {
	defaults := &runnerpb.SessionSpec{}
	var setupScript *string
	if presetName := sandbox.GetPreset(); presetName != "" {
		preset, ok := config.SandboxPresets[presetName]
		if !ok {
			return nil, nil, errors.New("Sandbox names preset " + quote(presetName) + ", but it is not in the app's preset catalog")
		}
		thread, ok := config.ThreadPresets[preset.ThreadPreset]
		if !ok {
			return nil, nil, errors.New("Sandbox preset " + quote(presetName) + " names an unknown ThreadPreset")
		}
		harness, ok := runnerpb.Harness_value[thread.Harness]
		if !ok {
			return nil, nil, errors.New("ThreadPreset names an unknown Harness " + quote(thread.Harness))
		}
		defaults.Harness = runnerpb.Harness(harness)
		defaults.Model = thread.Model
		defaults.Cwd = thread.CWD
		if thread.ReasoningEffort != nil {
			defaults.ReasoningEffort = *thread.ReasoningEffort
		}
		defaults.Instructions = thread.Instructions
		script := thread.SetupScript
		setupScript = &script
	}
	if setupWasSet {
		setupScript = requestedSetup
	}
	return defaults, setupScript, nil
}

func replaceSessionID(value, sessionID string) string {
	return strings.ReplaceAll(value, "{session_id}", sessionID)
}

func openingOutput(opening *dispatcherpb.SessionOpening) (*sessionOpeningOutput, error) {
	encoded, err := (protojson.MarshalOptions{}).Marshal(opening)
	if err != nil {
		return nil, huma.Error500InternalServerError("Session opening could not be encoded")
	}
	var result map[string]any
	if err := json.Unmarshal(encoded, &result); err != nil {
		return nil, huma.Error500InternalServerError("Session opening could not be decoded")
	}
	return &sessionOpeningOutput{Body: result}, nil
}

func sessionHTTPError(err error) error {
	var timeout *appbridge.DispatcherRequestTimeoutError
	var opening *appbridge.SessionOpeningNotFoundError
	var malformed *appbridge.MalformedMessageError
	var threadMissing *appbridge.ThreadNotFoundError
	var threadState *appbridge.ThreadStateError
	var refused *appbridge.RefusedError
	switch {
	case errors.As(err, &timeout):
		return huma.Error504GatewayTimeout(err.Error())
	case errors.As(err, &opening), errors.As(err, &threadMissing):
		return huma.Error404NotFound(err.Error())
	case errors.As(err, &malformed):
		return huma.Error422UnprocessableEntity(err.Error())
	case errors.As(err, &threadState), errors.As(err, &refused):
		return huma.Error409Conflict(err.Error())
	default:
		return sandboxHTTPError(err)
	}
}
