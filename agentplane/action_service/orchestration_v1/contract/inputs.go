// Package contract defines the orchestration_v1 Actions and the wire types that decode their inputs.
package contract

import (
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/invopop/jsonschema"
	"k8s.io/apimachinery/pkg/types"
)

const (
	ActionListLaunchPresets     = "list_launch_presets"
	ActionLaunchSandbox         = "launch_sandbox"
	ActionOpenSession           = "open_session"
	ActionSubmitInput           = "submit_input"
	ActionInterruptTurn         = "interrupt_turn"
	ActionChangeModel           = "change_model"
	ActionChangeReasoningEffort = "change_reasoning_effort"
	ActionReadTurns             = "read_turns"
	ActionWaitForTurn           = "wait_for_turn"
	ActionGetSession            = "get_session"
	ActionListSessions          = "list_sessions"
	GrantSendMessage            = "send_message"
	RecentDispatches            = 20
)

type NoArgs struct{}

type SandboxRef struct {
	Name string    `json:"name" jsonschema:"minLength=1,maxLength=57,pattern=^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"`
	UID  types.UID `json:"uid" jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
}

type Harness string

func (Harness) JSONSchema() *jsonschema.Schema {
	return &jsonschema.Schema{
		Type: "string",
		Enum: []any{runnerpb.Harness_HARNESS_CLAUDE.String(), runnerpb.Harness_HARNESS_CODEX.String()},
	}
}

type LaunchArgs struct {
	Name                    string   `json:"name" jsonschema_description:"The new Sandbox's name, a DNS label no Sandbox in the namespace has." jsonschema:"minLength=1,maxLength=57,pattern=^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"`
	Template                string   `json:"template" jsonschema_description:"The SandboxTemplate it runs from." jsonschema:"minLength=1"`
	PolicySets              []string `json:"policy_sets,omitempty" jsonschema_description:"PolicySets its own account is bound to, beside this deployment's defaults." jsonschema:"uniqueItems=true"`
	InboxTags               []string `json:"inbox_tags,omitempty" jsonschema_description:"Groups its Session inboxes take messages as." jsonschema:"uniqueItems=true"`
	Bootstrap               string   `json:"bootstrap,omitempty" jsonschema_description:"Shell source its runner runs once, before its first Session." jsonschema:"maxLength=65536"`
	WakeOnSend              bool     `json:"wake_on_send,omitempty" jsonschema_description:"Whether an inbox message or a submitted input resumes it once it is suspended."`
	IdleSuspendAfterSeconds *int     `json:"idle_suspend_after_seconds,omitempty" jsonschema_description:"How long its runner stays idle before it is suspended. Absent, it never is." jsonschema:"minimum=60,maximum=31536000"`
	LauncherGrants          []string "json:\"launcher_grants,omitempty\" jsonschema_description:\"What you may then do to this Sandbox without asking again, bound to your own account and deleted with the Sandbox: `send_message` to its Session inboxes, and each named Action of this group on it.\" jsonschema:\"uniqueItems=true\""
}

type OpenSessionArgs struct {
	Sandbox     SandboxRef  `json:"sandbox" jsonschema_description:"The Sandbox as launch_sandbox returned it: its name and the UID of that incarnation. A Sandbox deleted and launched again under the name is another one, and is refused."`
	SessionID   string      `json:"session_id" jsonschema_description:"A fresh UUID you mint for the Session. Repeating a call with the same ID and fields opens it once; the same ID with other fields is refused." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	Spec        SessionSpec `json:"spec" jsonschema_description:"What the Session runs: its harness, model and working directory, fixed when it opens."`
	SetupScript *string     `json:"setup_script,omitempty" jsonschema_description:"Shell source run once before the harness starts." jsonschema:"maxLength=65536"`
}

type SessionSpec struct {
	Harness         Harness `json:"harness"`
	Model           string  `json:"model" jsonschema:"minLength=1"`
	Cwd             string  "json:\"cwd\" jsonschema_description:\"The working directory; `{session_id}` in it becomes the Session's ID.\" jsonschema:\"minLength=1\""
	ReasoningEffort string  `json:"reasoning_effort,omitempty" jsonschema:"minLength=1"`
	Instructions    string  `json:"instructions,omitempty" jsonschema_description:"Standing instructions, fixed for the Session's life."`
}

type SubmitInputArgs struct {
	Sandbox   SandboxRef `json:"sandbox" jsonschema_description:"The Sandbox as launch_sandbox returned it: its name and the UID of that incarnation. A Sandbox deleted and launched again under the name is another one, and is refused."`
	SessionID string     `json:"session_id" jsonschema_description:"The Session's ID, as open_session was given it." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	CommandID string     `json:"command_id" jsonschema_description:"A fresh UUID you mint for this command. Repeating a call with the same ID and fields does not send it twice; the same ID with other fields is refused." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	Text      string     `json:"text" jsonschema:"minLength=1"`
}

type InterruptTurnArgs struct {
	Sandbox   SandboxRef `json:"sandbox" jsonschema_description:"The Sandbox as launch_sandbox returned it: its name and the UID of that incarnation. A Sandbox deleted and launched again under the name is another one, and is refused."`
	SessionID string     `json:"session_id" jsonschema_description:"The Session's ID, as open_session was given it." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	CommandID string     `json:"command_id" jsonschema_description:"A fresh UUID you mint for this command. Repeating a call with the same ID and fields does not send it twice; the same ID with other fields is refused." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	TurnID    string     `json:"turn_id" jsonschema_description:"The running turn to interrupt." jsonschema:"minLength=1"`
}

type ChangeModelArgs struct {
	Sandbox   SandboxRef `json:"sandbox" jsonschema_description:"The Sandbox as launch_sandbox returned it: its name and the UID of that incarnation. A Sandbox deleted and launched again under the name is another one, and is refused."`
	SessionID string     `json:"session_id" jsonschema_description:"The Session's ID, as open_session was given it." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	CommandID string     `json:"command_id" jsonschema_description:"A fresh UUID you mint for this command. Repeating a call with the same ID and fields does not send it twice; the same ID with other fields is refused." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	Model     string     `json:"model" jsonschema:"minLength=1"`
}

type ChangeReasoningEffortArgs struct {
	Sandbox   SandboxRef `json:"sandbox" jsonschema_description:"The Sandbox as launch_sandbox returned it: its name and the UID of that incarnation. A Sandbox deleted and launched again under the name is another one, and is refused."`
	SessionID string     `json:"session_id" jsonschema_description:"The Session's ID, as open_session was given it." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	CommandID string     `json:"command_id" jsonschema_description:"A fresh UUID you mint for this command. Repeating a call with the same ID and fields does not send it twice; the same ID with other fields is refused." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	Effort    string     `json:"effort" jsonschema:"minLength=1"`
}

type ReadTurnsArgs struct {
	Sandbox   SandboxRef `json:"sandbox" jsonschema_description:"The Sandbox as launch_sandbox returned it: its name and the UID of that incarnation. A Sandbox deleted and launched again under the name is another one, and is refused."`
	SessionID string     `json:"session_id" jsonschema_description:"The Session's ID, as open_session was given it." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	Count     int        `json:"count" jsonschema_description:"How many recent turns to read, oldest first." jsonschema:"minimum=1,maximum=100"`
}

type WaitForTurnArgs struct {
	Sandbox     SandboxRef `json:"sandbox" jsonschema_description:"The Sandbox as launch_sandbox returned it: its name and the UID of that incarnation. A Sandbox deleted and launched again under the name is another one, and is refused."`
	SessionID   string     `json:"session_id" jsonschema_description:"The Session's ID, as open_session was given it." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
	ResumeToken string     `json:"resume_token" jsonschema_description:"The opaque token returned by read_turns or wait_for_turn for this Session." jsonschema:"minLength=1,maxLength=2048"`
	WaitSeconds float64    `json:"wait_seconds" jsonschema_description:"Maximum wait, also capped below the deployment direct-tool budget." jsonschema:"exclusiveMinimum=0,maximum=30"`
}

type GetSessionArgs struct {
	Sandbox   SandboxRef `json:"sandbox" jsonschema_description:"The Sandbox as launch_sandbox returned it: its name and the UID of that incarnation. A Sandbox deleted and launched again under the name is another one, and is refused."`
	SessionID string     `json:"session_id" jsonschema_description:"The Session's ID, as open_session was given it." jsonschema:"pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"`
}

type ListSessionsArgs struct {
	Sandbox SandboxRef `json:"sandbox" jsonschema_description:"The Sandbox as launch_sandbox returned it: its name and the UID of that incarnation. A Sandbox deleted and launched again under the name is another one, and is refused."`
}

type Inputs struct {
	ListLaunchPresets     catalog.Input[NoArgs]
	LaunchSandbox         catalog.Input[LaunchArgs]
	OpenSession           catalog.Input[OpenSessionArgs]
	SubmitInput           catalog.Input[SubmitInputArgs]
	InterruptTurn         catalog.Input[InterruptTurnArgs]
	ChangeModel           catalog.Input[ChangeModelArgs]
	ChangeReasoningEffort catalog.Input[ChangeReasoningEffortArgs]
	ReadTurns             catalog.Input[ReadTurnsArgs]
	WaitForTurn           catalog.Input[WaitForTurnArgs]
	GetSession            catalog.Input[GetSessionArgs]
	ListSessions          catalog.Input[ListSessionsArgs]
}

func NewInputs() (Inputs, error) {
	var inputs Inputs
	var err error
	sandboxNote := fmt.Sprintf("The Sandbox is the one %s returned, by name and UID.", ActionLaunchSandbox)
	commandNote := "Answers with the command's Dispatch once the Session Dispatcher has queued it, not once it ran; a " +
		"suspended Sandbox with wake_on_send resumes for it. " + sandboxNote
	if inputs.ListLaunchPresets, err = newInput[NoArgs](ActionListLaunchPresets, fmt.Sprintf("The reviewed launch presets, each as the %s and %s fields it stands for. Copy them into those Actions' arguments, adding your own name, launcher_grants, sandbox and session_id: a preset is not an argument of either, so what you launch is what you name.", ActionLaunchSandbox, ActionOpenSession), "List launch presets", &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)}, nil); err != nil {
		return Inputs{}, err
	}
	if inputs.OpenSession, err = newInput[OpenSessionArgs](ActionOpenSession, "Open a Session in a launched Sandbox, under an ID you mint, with its spec and setup script. Answers with the opening once the Session Dispatcher has queued it; it is delivered once the Sandbox is Ready, before any command for the Session.", "Open Session", &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)}, nil); err != nil {
		return Inputs{}, err
	}
	if inputs.SubmitInput, err = newInput[SubmitInputArgs](ActionSubmitInput, "Send a user message to a Session. "+commandNote, "Submit Session input", &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)}, nil); err != nil {
		return Inputs{}, err
	}
	if inputs.InterruptTurn, err = newInput[InterruptTurnArgs](ActionInterruptTurn, "Interrupt a Session's running turn. Delivered at once rather than queued, so it reaches only a Ready Sandbox; answers with whether the runner admitted it or found nothing to do. "+sandboxNote, "Interrupt Session turn", &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(true), IdempotentHint: new(true)}, nil); err != nil {
		return Inputs{}, err
	}
	if inputs.ChangeModel, err = newInput[ChangeModelArgs](ActionChangeModel, "Change the model a Session's next turns run on. "+commandNote, "Change Session model", &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)}, nil); err != nil {
		return Inputs{}, err
	}
	if inputs.ChangeReasoningEffort, err = newInput[ChangeReasoningEffortArgs](ActionChangeReasoningEffort, "Change the reasoning effort of a Session's next turns. "+commandNote, "Change Session reasoning effort", &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)}, nil); err != nil {
		return Inputs{}, err
	}
	if inputs.ReadTurns, err = newInput[ReadTurnsArgs](ActionReadTurns, "Read the last count turns in cursor order, including confirmed input, tool names, status and final assistant message. Returns an opaque resume_token for wait_for_turn. "+sandboxNote, "Read Session turns", &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)}, nil); err != nil {
		return Inputs{}, err
	}
	if inputs.WaitForTurn, err = newInput[WaitForTurnArgs](ActionWaitForTurn, "Wait for the first turn completion after resume_token, for at most wait_seconds within this deployment's direct-tool budget. Returns completed=false with the newest token when still running; repeat with that token. Read full messages with read_turns. "+sandboxNote, "Wait for Session turn", &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)}, nil); err != nil {
		return Inputs{}, err
	}
	if inputs.GetSession, err = newInput[GetSessionArgs](ActionGetSession, fmt.Sprintf("A Session in a launched Sandbox: whether the Sandbox is Ready; the Session as its history records it, including whether a turn runs (its ID is what %s takes), how the last one ended and since when the harness is idle; its opening; and its %d most recent commands' Dispatches, newest first. %s", ActionInterruptTurn, RecentDispatches, sandboxNote), "Get Session", &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)}, nil); err != nil {
		return Inputs{}, err
	}
	if inputs.ListSessions, err = newInput[ListSessionsArgs](ActionListSessions, fmt.Sprintf("Every Session a launched Sandbox has written, newest first, as %s reads each from its history. A Session is listed once its runner has written it; %s shows one still queued. %s", ActionGetSession, ActionGetSession, sandboxNote), "List Sessions", &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)}, nil); err != nil {
		return Inputs{}, err
	}
	if inputs.LaunchSandbox, err = newInput[LaunchArgs](ActionLaunchSandbox, fmt.Sprintf("Launch a Session Sandbox running from a template, with its own ServiceAccount bound to the PolicySets named and this deployment's defaults, and grant yourself what launcher_grants names on it. Answers with its name and UID once it exists, before it is Ready; %s queues until it is. Repeating the same launch as the same caller answers with the same Sandbox; any other launch of a taken name is refused.", ActionOpenSession), "Launch Session Sandbox", &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)}, configureLauncherGrants(inputs.SandboxActions())); err != nil {
		return Inputs{}, err
	}
	return inputs, nil
}

func newInput[T any](name, description, title string, annotations *catalog.ToolAnnotations, configure func(*jsonschema.Schema) error) (catalog.Input[T], error) {
	input, err := catalog.NewInput[T](catalog.InputOptions{
		Name: name, Description: description, Title: new(title), Annotations: annotations, Configure: configure,
	})
	if err != nil {
		return input, fmt.Errorf("%s input: %w", name, err)
	}
	return input, nil
}

func configureLauncherGrants(grants []string) func(*jsonschema.Schema) error {
	return func(schema *jsonschema.Schema) error {
		property, ok := schema.Properties.Get("launcher_grants")
		if !ok || property.Items == nil {
			return fmt.Errorf("launcher_grants is absent from the reflected input")
		}
		property.Items.Enum = []any{GrantSendMessage}
		for _, grant := range grants {
			property.Items.Enum = append(property.Items.Enum, grant)
		}
		policySets, ok := schema.Properties.Get("policy_sets")
		if !ok || policySets.Items == nil {
			return fmt.Errorf("policy_sets is absent from the reflected input")
		}
		policySets.Items.MinLength = new(uint64(1))
		inboxTags, ok := schema.Properties.Get("inbox_tags")
		if !ok || inboxTags.Items == nil {
			return fmt.Errorf("inbox_tags is absent from the reflected input")
		}
		inboxTags.Items.MinLength = new(uint64(1))
		inboxTags.Items.MaxLength = new(uint64(63))
		return nil
	}
}

func (inputs Inputs) Actions() map[string]catalog.ActionDefinition {
	return map[string]catalog.ActionDefinition{
		inputs.ListLaunchPresets.Name():     inputs.ListLaunchPresets.Definition(),
		inputs.LaunchSandbox.Name():         inputs.LaunchSandbox.Definition(),
		inputs.OpenSession.Name():           inputs.OpenSession.Definition(),
		inputs.SubmitInput.Name():           inputs.SubmitInput.Definition(),
		inputs.InterruptTurn.Name():         inputs.InterruptTurn.Definition(),
		inputs.ChangeModel.Name():           inputs.ChangeModel.Definition(),
		inputs.ChangeReasoningEffort.Name(): inputs.ChangeReasoningEffort.Definition(),
		inputs.ReadTurns.Name():             inputs.ReadTurns.Definition(),
		inputs.WaitForTurn.Name():           inputs.WaitForTurn.Definition(),
		inputs.GetSession.Name():            inputs.GetSession.Definition(),
		inputs.ListSessions.Name():          inputs.ListSessions.Definition(),
	}
}

func (inputs Inputs) SandboxActions() []string {
	return []string{
		inputs.OpenSession.Name(), inputs.SubmitInput.Name(), inputs.InterruptTurn.Name(), inputs.ChangeModel.Name(),
		inputs.ChangeReasoningEffort.Name(), inputs.GetSession.Name(), inputs.ListSessions.Name(),
		inputs.ReadTurns.Name(), inputs.WaitForTurn.Name(),
	}
}
