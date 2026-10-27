// Package orchestrationv1 runs the orchestration_v1 Actions in this service: launching Session Sandboxes with grants
// for their launcher, and opening and driving their Sessions through the Session Dispatcher. Each runs on its request's
// Decision, under this service's own authority; README.md says what that delegates.
package orchestrationv1

import (
	_ "embed"
	"encoding/json"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
)

// The Actions an orchestration_v1 group offers.
const (
	actionListLaunchPresets     = "list_launch_presets"
	actionLaunchSandbox         = "launch_sandbox"
	actionOpenSession           = "open_session"
	actionSubmitInput           = "submit_input"
	actionInterruptTurn         = "interrupt_turn"
	actionChangeModel           = "change_model"
	actionChangeReasoningEffort = "change_reasoning_effort"
)

// grantSendMessage is the launcher grant of the Notification Service's send_message on the launched Sandbox's Session
// inboxes; every other launcher grant names an Action of this group that takes the Sandbox.
const grantSendMessage = "send_message"

// sandboxActions are this group's Actions on one Sandbox, which a launcher grant can name; their arguments hold it as
// `sandbox`.
var sandboxActions = []string{
	actionOpenSession, actionSubmitInput, actionInterruptTurn, actionChangeModel, actionChangeReasoningEffort,
}

// inputSchemas are the JSON Schemas of the Actions' arguments, by the name of the model they describe.
//
//go:embed input_schemas.json
var inputSchemas []byte

// argumentModels names each Action's arguments' schema in inputSchemas.
var argumentModels = map[string]string{
	actionListLaunchPresets:     "NoArgs",
	actionLaunchSandbox:         "LaunchArgs",
	actionOpenSession:           "OpenSessionArgs",
	actionSubmitInput:           "SubmitInputArgs",
	actionInterruptTurn:         "InterruptTurnArgs",
	actionChangeModel:           "ChangeModelArgs",
	actionChangeReasoningEffort: "ChangeReasoningEffortArgs",
}

// schemas reads inputSchemas.
func schemas() map[string]map[string]any {
	var models map[string]map[string]any
	if err := json.Unmarshal(inputSchemas, &models); err != nil {
		panic(err)
	}
	schemas := make(map[string]map[string]any, len(argumentModels))
	for action, model := range argumentModels {
		schemas[action] = models[model]
	}
	return schemas
}

// Actions is what an orchestration_v1 group offers, declared here rather than discovered from a backend.
func Actions() map[string]catalog.ActionDefinition {
	commandNote := fmt.Sprintf("Answers with the command's Dispatch once the Session Dispatcher has queued it, not once "+
		"it ran; a suspended Sandbox with wake_on_send resumes for it. The Sandbox is the one %s returned, by name and "+
		"UID.", actionLaunchSandbox)
	definitions := map[string]catalog.ActionDefinition{
		actionListLaunchPresets: {
			Description: fmt.Sprintf("The reviewed launch presets, each as the %s and %s fields it stands for. Copy them "+
				"into those Actions' arguments, adding your own name, launcher_grants, sandbox and session_id: a preset is "+
				"not an argument of either, so what you launch is what you name.", actionLaunchSandbox, actionOpenSession),
			Title:       new("List launch presets"),
			Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)},
		},
		actionLaunchSandbox: {
			Description: fmt.Sprintf("Launch a Session Sandbox running from a template, with its own ServiceAccount bound "+
				"to the PolicySets named and this deployment's defaults, and grant yourself what launcher_grants names on "+
				"it. Answers with its name and UID once it exists, before it is Ready; %s queues until it is. Repeating "+
				"the same launch as the same caller answers with the same Sandbox; any other launch of a taken name is "+
				"refused.", actionOpenSession),
			Title:       new("Launch Session Sandbox"),
			Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)},
		},
		actionOpenSession: {
			Description: "Open a Session in a launched Sandbox, under an ID you mint, with its spec and setup script. " +
				"Answers with the opening once the Session Dispatcher has queued it; it is delivered once the Sandbox " +
				"is Ready, before any command for the Session.",
			Title:       new("Open Session"),
			Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)},
		},
		actionSubmitInput: {
			Description: "Send a user message to a Session. " + commandNote,
			Title:       new("Submit Session input"),
			Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)},
		},
		actionInterruptTurn: {
			Description: fmt.Sprintf("Interrupt a Session's running turn. Delivered at once rather than queued, so it "+
				"reaches only a Ready Sandbox; answers with whether the runner admitted it or found nothing to do. The "+
				"Sandbox is the one %s returned, by name and UID.", actionLaunchSandbox),
			Title:       new("Interrupt Session turn"),
			Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(true), IdempotentHint: new(true)},
		},
		actionChangeModel: {
			Description: "Change the model a Session's next turns run on. " + commandNote,
			Title:       new("Change Session model"),
			Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)},
		},
		actionChangeReasoningEffort: {
			Description: "Change the reasoning effort of a Session's next turns. " + commandNote,
			Title:       new("Change Session reasoning effort"),
			Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)},
		},
	}
	inputs := schemas()
	for action, definition := range definitions {
		schema, err := json.Marshal(inputs[action])
		if err != nil {
			panic(err)
		}
		definition.InputSchema = schema
		definitions[action] = definition
	}
	return definitions
}
