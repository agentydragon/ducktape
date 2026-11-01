// Package contract defines the reviewed same-principal environment and Session Actions.
package contract

import (
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/invopop/jsonschema"
)

const (
	GroupEnvironments = "environments"
	GroupSessions     = "sessions"

	ActionListEnvironments = "list"
	ActionGetEnvironment   = "get"

	ActionOpenSession   = "open"
	ActionSubmitInput   = "submit_input"
	ActionObserveInput  = "observe_input"
	ActionGetSession    = "get"
	ActionReadTurns     = "read_turns"
	ActionCancelInput   = "cancel_input"
	ActionInterruptTurn = "interrupt_turn"
)

// Harness is a named protobuf Harness enum value, as JSON Schema exposes it to a tool caller.
type Harness string

func (Harness) JSONSchema() *jsonschema.Schema {
	return &jsonschema.Schema{
		Type: "string",
		Enum: []any{
			sessionpb.Harness_HARNESS_CLAUDE.String(),
			sessionpb.Harness_HARNESS_CODEX.String(),
			sessionpb.Harness_HARNESS_CENTRAL_SAMPLER.String(),
		},
	}
}

func (h Harness) Proto() (sessionpb.Harness, error) {
	value, ok := sessionpb.Harness_value[string(h)]
	if !ok || value == int32(sessionpb.Harness_HARNESS_UNSPECIFIED) {
		return sessionpb.Harness_HARNESS_UNSPECIFIED, fmt.Errorf("unsupported harness enum value")
	}
	return sessionpb.Harness(value), nil
}

type TargetRef struct {
	Namespace string `json:"namespace" jsonschema:"minLength=1,maxLength=63,pattern=^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"`
	Name      string `json:"name" jsonschema:"minLength=1,maxLength=253"`
	UID       string `json:"uid" jsonschema_description:"The exact Kubernetes Sandbox UID returned by environments__list or environments__get." jsonschema:"minLength=1"`
}

type ListEnvironmentsArgs struct {
	Limit int     `json:"limit" jsonschema:"minimum=1,maximum=100"`
	After *string `json:"after,omitempty" jsonschema_description:"Opaque continuation from the previous environments__list page." jsonschema:"minLength=1"`
}

type GetEnvironmentArgs struct {
	Target TargetRef `json:"target"`
}

type SessionSpec struct {
	Harness         Harness `json:"harness"`
	Model           string  `json:"model" jsonschema:"minLength=1"`
	Cwd             string  `json:"cwd" jsonschema_description:"Absolute working directory for the Session." jsonschema:"minLength=1"`
	ReasoningEffort string  `json:"reasoning_effort,omitempty" jsonschema:"minLength=1"`
}

type OpenSessionArgs struct {
	Target                 TargetRef   `json:"target"`
	SessionID              string      `json:"session_id" jsonschema_description:"Caller-minted UUID. Repeating this exact request recovers the original opening; conflicting reuse fails." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	Spec                   SessionSpec `json:"spec"`
	AdditionalInstructions string      `json:"additional_instructions,omitempty" jsonschema_description:"Standing instructions added to the Session. This field does not submit input or start a model turn. After opening, call sessions__submit_input with the task to start work."`
	SetupScript            *string     `json:"setup_script,omitempty" jsonschema:"maxLength=65536"`
}

type SubmitInputArgs struct {
	Target    TargetRef `json:"target"`
	SessionID string    `json:"session_id" jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	CommandID string    `json:"command_id" jsonschema_description:"Caller-minted UUID. Reuse it only to recover this exact command." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	Text      string    `json:"text" jsonschema:"minLength=1"`
}

type ObserveInputArgs struct {
	SessionID   string `json:"session_id" jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	CommandID   string `json:"command_id" jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	WaitSeconds int    `json:"wait_seconds" jsonschema:"minimum=0,maximum=30"`
	ResumeToken string `json:"resume_token,omitempty" jsonschema:"minLength=1,maxLength=2048"`
}

type GetSessionArgs struct {
	SessionID string `json:"session_id" jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
}

type ReadTurnsArgs struct {
	SessionID  string  `json:"session_id" jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	Count      int     `json:"count" jsonschema:"minimum=1,maximum=100"`
	PageCursor *string `json:"page_cursor,omitempty" jsonschema_description:"Opaque page cursor returned by the prior sessions__read_turns call." jsonschema:"minLength=1"`
}

type CancelInputArgs struct {
	SessionID string `json:"session_id" jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	CommandID string `json:"command_id" jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
}

type InterruptTurnArgs struct {
	Target    TargetRef `json:"target"`
	SessionID string    `json:"session_id" jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	CommandID string    `json:"command_id" jsonschema_description:"Fresh caller-minted UUID for this immediate interruption." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	TurnID    string    `json:"turn_id" jsonschema_description:"The exact active turn returned by a Session observation." jsonschema:"minLength=1"`
}

type Inputs struct {
	ListEnvironments catalog.Input[ListEnvironmentsArgs]
	GetEnvironment   catalog.Input[GetEnvironmentArgs]
	OpenSession      catalog.Input[OpenSessionArgs]
	SubmitInput      catalog.Input[SubmitInputArgs]
	ObserveInput     catalog.Input[ObserveInputArgs]
	GetSession       catalog.Input[GetSessionArgs]
	ReadTurns        catalog.Input[ReadTurnsArgs]
	CancelInput      catalog.Input[CancelInputArgs]
	InterruptTurn    catalog.Input[InterruptTurnArgs]
}

func NewInputs() (Inputs, error) {
	var inputs Inputs
	var err error
	if inputs.ListEnvironments, err = newInput[ListEnvironmentsArgs](ActionListEnvironments,
		"Find existing environments whose execution account matches your authenticated account and can host Sessions. Use this first when choosing a target; it never creates or wakes an environment. Provide a page limit and, when continuing, the opaque `after` token from the prior page. The result contains eligible target references and runtime choices; an empty page is valid and a source failure is reported as an error.",
		"List environments", &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)}); err != nil {
		return Inputs{}, err
	}
	if inputs.GetEnvironment, err = newInput[GetEnvironmentArgs](ActionGetEnvironment,
		"Refresh one existing environment and its current Session configuration. Supply the complete target reference, including the UID returned by `environments__list`; a same-name replacement is a different environment and is refused. This read creates nothing and does not wake the target. The result is the current descriptor, or a structured error if the exact incarnation is gone or unavailable.",
		"Get environment", &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)}); err != nil {
		return Inputs{}, err
	}
	if inputs.OpenSession, err = newInput[OpenSessionArgs](ActionOpenSession,
		"Durably open a Session in an exact existing environment whose execution account matches your authenticated account. Supply a caller-minted `session_id`, offered runtime settings, and the target's full UID; `additional_instructions` only adds standing guidance and never starts a turn. Opening does not submit task input, so call `sessions__submit_input` separately to start work. The result is the typed Dispatcher opening receipt; after an uncertain reply, look it up by the same Session ID rather than minting another ID.",
		"Open session", &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)}); err != nil {
		return Inputs{}, err
	}
	if inputs.SubmitInput, err = newInput[SubmitInputArgs](ActionSubmitInput,
		"Durably submit one message to a Session in the exact target incarnation. Supply a fresh caller-minted `command_id` and reuse it only to recover this same command; the target and Session must belong to your authenticated account. The typed Dispatch receipt means stored or queued, not consumed by the agent. If the reply is uncertain, call `sessions__observe_input` with the same Session and command IDs; this Action never replays the submission.",
		"Submit session input", &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)}); err != nil {
		return Inputs{}, err
	}
	if inputs.ObserveInput, err = newInput[ObserveInputArgs](ActionObserveInput,
		"Reconcile one caller-held input command by its Session and command IDs. Choose a wait from 0 to 30 seconds and pass the returned opaque resume token to continue observing; a timeout only returns facts so far. The structured result keeps Dispatcher and History evidence separate, including gaps and source errors. This read neither cancels nor replays the command, and it does not require the environment to remain live.",
		"Observe session input", &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)}); err != nil {
		return Inputs{}, err
	}
	if inputs.GetSession, err = newInput[GetSessionArgs](ActionGetSession,
		"Read the retained opening and History snapshot for a Session you are authorized to inspect. Supply its Session UUID; no live environment is required. The structured response distinguishes observed, not-observed, unavailable, and partial source facts instead of collapsing a failed read into an empty result. Use `sessions__read_turns` for paginated compact turns.",
		"Get session", &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)}); err != nil {
		return Inputs{}, err
	}
	if inputs.ReadTurns, err = newInput[ReadTurnsArgs](ActionReadTurns,
		"Read retained compact Session turns in cursor order for a Session you are authorized to inspect. Choose a page count from 1 to 100 and pass the opaque page cursor from the prior page to continue. The cursor is Session-scoped and pins the read to the history tail first observed, so later events do not shift pages. Source gaps or read errors remain explicit in the typed page result.",
		"Read session turns", &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)}); err != nil {
		return Inputs{}, err
	}
	if inputs.CancelInput, err = newInput[CancelInputArgs](ActionCancelInput,
		"Cancel an input only while its exact Dispatch is still queued and before delivery starts. Supply both the Session UUID and command UUID; the backend receipt must show that the command belongs to this Session and is a submitted input. The result is the typed Dispatcher receipt, and a stale or mismatched command is refused. If the reply is uncertain, call `sessions__observe_input`; this Action never retries cancellation.",
		"Cancel queued input", &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(true), IdempotentHint: new(true)}); err != nil {
		return Inputs{}, err
	}
	if inputs.InterruptTurn, err = newInput[InterruptTurnArgs](ActionInterruptTurn,
		"Immediately interrupt exactly the active turn you observed in a Ready environment. Supply the full target UID, Session UUID, opaque turn ID, and a fresh caller-minted command UUID; the backend never redirects a stale turn ID to its successor. The typed Dispatcher response reports the existing admission/state facts, not task success. If the reply is uncertain, inspect the same turn before requesting another interruption.",
		"Interrupt session turn", &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(true), IdempotentHint: new(false)}); err != nil {
		return Inputs{}, err
	}
	return inputs, nil
}

func newInput[T any](name, description, title string, annotations *catalog.ToolAnnotations) (catalog.Input[T], error) {
	input, err := catalog.NewInput[T](catalog.InputOptions{
		Name: name, Description: description, Title: new(title), Annotations: annotations,
	})
	if err != nil {
		return input, fmt.Errorf("%s input: %w", name, err)
	}
	return input, nil
}

func (inputs Inputs) EnvironmentActions() map[string]catalog.ActionDefinition {
	return map[string]catalog.ActionDefinition{
		inputs.ListEnvironments.Name(): inputs.ListEnvironments.Definition(),
		inputs.GetEnvironment.Name():   inputs.GetEnvironment.Definition(),
	}
}

func (inputs Inputs) SessionActions() map[string]catalog.ActionDefinition {
	return map[string]catalog.ActionDefinition{
		inputs.OpenSession.Name():   inputs.OpenSession.Definition(),
		inputs.SubmitInput.Name():   inputs.SubmitInput.Definition(),
		inputs.ObserveInput.Name():  inputs.ObserveInput.Definition(),
		inputs.GetSession.Name():    inputs.GetSession.Definition(),
		inputs.ReadTurns.Name():     inputs.ReadTurns.Definition(),
		inputs.CancelInput.Name():   inputs.CancelInput.Definition(),
		inputs.InterruptTurn.Name(): inputs.InterruptTurn.Definition(),
	}
}

// SessionMutationActions are the Session operations currently implemented by the executor. Retained snapshot and
// turn projections are deliberately not offered until their composition adapters are present.
func (inputs Inputs) SessionMutationActions() map[string]catalog.ActionDefinition {
	return map[string]catalog.ActionDefinition{
		inputs.OpenSession.Name():   inputs.OpenSession.Definition(),
		inputs.SubmitInput.Name():   inputs.SubmitInput.Definition(),
		inputs.ObserveInput.Name():  inputs.ObserveInput.Definition(),
		inputs.CancelInput.Name():   inputs.CancelInput.Definition(),
		inputs.InterruptTurn.Name(): inputs.InterruptTurn.Definition(),
	}
}
