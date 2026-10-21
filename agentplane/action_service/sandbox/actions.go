// Package sandbox runs the sandbox Actions in this service: boxes a caller creates, runs Bash scripts in, reads and
// disposes of, each a Pod that runs as the ServiceAccount that called. README.md says why a box is safe to offer.
package sandbox

import (
	_ "embed"
	"encoding/json"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
)

// The Actions a sandbox group offers.
const (
	actionCreate      = "create"
	actionGetTemplate = "get_template"
	actionExec        = "exec"
	actionList        = "list"
	actionGet         = "get"
	actionDispose     = "dispose"
)

// readyCondition is the Pod condition that says a box can run something: exec requires it, and the Action
// descriptions tell an agent to poll it.
const readyCondition = "Ready"

// inputSchemas are the JSON Schemas of the Actions' arguments, by the name of the model they describe.
//
//go:embed input_schemas.json
var inputSchemas []byte

// argumentModels names each Action's arguments' schema in inputSchemas.
var argumentModels = map[string]string{
	actionCreate:      "CreateArgs",
	actionGetTemplate: "TemplateArgs",
	actionExec:        "ExecArgs",
	actionList:        "NoArgs",
	actionGet:         "NameArgs",
	actionDispose:     "NameArgs",
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

// Actions is what a sandbox group offers, declared here rather than discovered from a backend.
//
// The offered templates, with what each says of itself, are in create's description, so an agent can choose one
// without another call: naming a template that is not offered is the likeliest way to get create wrong.
func Actions(binding catalog.PodBinding) map[string]catalog.ActionDefinition {
	offered, err := json.Marshal(binding.Templates)
	if err != nil {
		panic(err)
	}
	inputs := schemas()
	// exec's schema carries this deployment's caps as its maxima, so submission refuses a request the executor
	// would cut short, and says why, instead of the run ending early.
	execProperties := inputs[actionExec]["properties"].(map[string]any)
	execProperties["timeout_seconds"].(map[string]any)["maximum"] = binding.MaxTimeoutSeconds
	execProperties["max_output_bytes"].(map[string]any)["maximum"] = binding.MaxOutputBytes
	definitions := map[string]catalog.ActionDefinition{
		actionCreate: {
			Description: fmt.Sprintf("Create or reach a sandbox that runs as your own ServiceAccount. Returns as soon as its "+
				"Pod exists, before the box can run anything, so poll %s until its %q condition has status \"True\". "+
				"Idempotent on the name, so polling with %s would also work but tells you nothing more. Templates: %s. "+
				"%s shows one whole.", actionGet, readyCondition, actionCreate, offered, actionGetTemplate),
			Title:       new("Create sandbox"),
			Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)},
		},
		actionGetTemplate: {
			Description: fmt.Sprintf("One template that %s offers, whole: the Pod each box made from it gets, with its "+
				"images, resources, working directory, environment and volumes. %s adds the Pod's name and labels, and "+
				"runs the box as your ServiceAccount.", actionCreate, actionCreate),
			Title:       new("Show sandbox template"),
			Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)},
		},
		actionExec: {
			Description: "Run one bounded Bash script in a ready sandbox of yours. A nonzero exit is a normal result, " +
				"not a failure.",
			Title:       new("Run script in sandbox"),
			Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(true), IdempotentHint: new(false)},
		},
		actionList: {
			Description: "Every sandbox you have here. Another account's are not listed and not reachable.",
			Title:       new("List sandboxes"),
			Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)},
		},
		actionGet: {
			Description: fmt.Sprintf("Inspect one sandbox of yours without changing it: its Pod's phase and conditions, "+
				"verbatim. Poll it until %q has status \"True\"; until then that condition's reason and message say "+
				"what it is waiting on.", readyCondition),
			Title:       new("Inspect sandbox"),
			Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)},
		},
		actionDispose: {
			Description: "Delete one sandbox of yours, and everything in it. Disposing an absent one is not an error.",
			Title:       new("Dispose sandbox"),
			Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(true), IdempotentHint: new(true)},
		},
	}
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
