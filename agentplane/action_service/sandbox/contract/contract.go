// Package contract defines the Sandbox Actions' wire inputs, roster, and MCP metadata.
package contract

import (
	"encoding/json"
	"fmt"
	"strconv"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/invopop/jsonschema"
)

// GroupName is the catalog group that owns these Actions.
const GroupName = "sandbox"

// Action is one Action name in the Sandbox roster.
type Action string

const (
	Create      Action = "create"
	GetTemplate Action = "get_template"
	Exec        Action = "exec"
	List        Action = "list"
	Get         Action = "get"
	Dispose     Action = "dispose"
)

// Roster returns the Actions this group offers in its stable presentation order.
func Roster() []Action {
	inputs, err := Bind(catalog.PodBinding{MaxTimeoutSeconds: maxTimeoutSeconds, MaxOutputBytes: maxOutputBytes})
	if err != nil {
		panic(fmt.Errorf("bind Sandbox Action roster: %w", err))
	}
	return inputs.Roster()
}

const (
	ReadyCondition      = "Ready"
	maxTimeoutSeconds   = 3600
	maxOutputBytes      = 1_000_000
	sandboxNamePattern  = `^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`
	nameArgsDescription = "The whole argument of an Action that names one sandbox and nothing else."
)

// SandboxName carries the name bounds and DNS-label pattern shared by Sandbox Actions.
type SandboxName string

func (SandboxName) JSONSchema() *jsonschema.Schema {
	minimum, maximum := uint64(1), uint64(48)
	return &jsonschema.Schema{
		Type:      "string",
		MinLength: &minimum,
		MaxLength: &maximum,
		Pattern:   sandboxNamePattern,
	}
}

// CreateArgs is the input to create.
type CreateArgs struct {
	Name     SandboxName `json:"name" jsonschema_description:"DNS-label name for the sandbox, unique among this caller's. Reuse it to reach the same box again; choose another for an independent one."`
	Template string      `json:"template" jsonschema_description:"Which template to create the box from. Only the templates this deployment offers can be named."`
}

// TemplateArgs is the input to get_template.
type TemplateArgs struct {
	Template string "json:\"template\" jsonschema_description:\"Which template to show; only the ones `create` offers.\""
}

// ExecArgs is the input to exec. Decode uses the stable group ceilings; the advertised input uses the current
// binding's smaller maxima. Existing admitted requests can therefore still reach execution and be clipped.
type ExecArgs struct {
	Name           SandboxName     `json:"name" jsonschema_description:"DNS-label name for the sandbox, unique among this caller's. Reuse it to reach the same box again; choose another for an independent one."`
	Script         string          "json:\"script\" jsonschema:\"minLength=1\" jsonschema_description:\"Bash script text run with `bash -lc`. Pipes, redirects, globs, variable expansion and compound commands use normal Bash semantics.\""
	TimeoutSeconds catalog.Integer `json:"timeout_seconds" jsonschema:"exclusiveMinimum=0" jsonschema_description:"Wall-clock ceiling for the script."`
	MaxOutputBytes catalog.Integer `json:"max_output_bytes" jsonschema:"minimum=0" jsonschema_description:"Maximum bytes retained independently from stdout and stderr."`
	Cwd            *string         `json:"cwd,omitempty" jsonschema:"nullable" jsonschema_description:"Working directory; omit to start in the container's own working directory."`
}

// NoArgs is the empty input to list.
type NoArgs struct{}

// NameArgs is the input to get and dispose.
type NameArgs struct {
	Name SandboxName `json:"name" jsonschema_description:"DNS-label name for the sandbox, unique among this caller's. Reuse it to reach the same box again; choose another for an independent one."`
}

// Inputs are the Sandbox contracts bound to one executor configuration. Exec is the advertised profile; DecodeExec
// retains the stable contract accepted for requests admitted before a binding's caps changed.
type Inputs struct {
	Create      catalog.Input[CreateArgs]
	GetTemplate catalog.Input[TemplateArgs]
	Exec        catalog.Input[ExecArgs]
	DecodeExec  catalog.Input[ExecArgs]
	List        catalog.Input[NoArgs]
	Get         catalog.Input[NameArgs]
	Dispose     catalog.Input[NameArgs]
}

type actionDefinition interface {
	Name() string
	Definition() catalog.ActionDefinition
}

func (inputs Inputs) actionInputs() []actionDefinition {
	return []actionDefinition{inputs.Create, inputs.GetTemplate, inputs.Exec, inputs.List, inputs.Get, inputs.Dispose}
}

// Roster lists these inputs in the order they are presented to clients.
func (inputs Inputs) Roster() []Action {
	roster := make([]Action, 0, len(inputs.actionInputs()))
	for _, input := range inputs.actionInputs() {
		roster = append(roster, Action(input.Name()))
	}
	return roster
}

// Bind builds every schema from its typed input and binds exec's advertised maxima to this deployment.
func Bind(binding catalog.PodBinding) (Inputs, error) {
	offered, err := json.Marshal(binding.Templates)
	if err != nil {
		return Inputs{}, err
	}
	create, err := catalog.NewInput[CreateArgs](catalog.InputOptions{
		Name: string(Create),
		Description: fmt.Sprintf("Create or reach a sandbox that runs as your own ServiceAccount. Returns as soon as its "+
			"Pod exists, before the box can run anything, so poll %s until its %q condition has status \"True\". "+
			"Idempotent on the name, so polling with %s would also work but tells you nothing more. Templates: %s. "+
			"%s shows one whole.", Get, ReadyCondition, Create, offered, GetTemplate),
		Title:       new("Create sandbox"),
		Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(false), IdempotentHint: new(true)},
	})
	if err != nil {
		return Inputs{}, err
	}
	getTemplate, err := catalog.NewInput[TemplateArgs](catalog.InputOptions{
		Name: string(GetTemplate),
		Description: fmt.Sprintf("One template that %s offers, whole: the Pod each box made from it gets, with its "+
			"images, resources, working directory, environment and volumes. %s adds the Pod's name and labels, and "+
			"runs the box as your ServiceAccount.", Create, Create),
		Title:       new("Show sandbox template"),
		Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)},
	})
	if err != nil {
		return Inputs{}, err
	}
	exec, err := newExecInput(binding.MaxTimeoutSeconds, binding.MaxOutputBytes)
	if err != nil {
		return Inputs{}, err
	}
	decodeExec, err := newExecInput(maxTimeoutSeconds, maxOutputBytes)
	if err != nil {
		return Inputs{}, err
	}
	list, err := catalog.NewInput[NoArgs](catalog.InputOptions{
		Name:        string(List),
		Description: "Every sandbox you have here. Another account's are not listed and not reachable.",
		Title:       new("List sandboxes"),
		Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)},
	})
	if err != nil {
		return Inputs{}, err
	}
	get, err := catalog.NewInput[NameArgs](nameOptions(Get,
		fmt.Sprintf("Inspect one sandbox of yours without changing it: its Pod's phase and conditions, "+
			"verbatim. Poll it until %q has status \"True\"; until then that condition's reason and message say "+
			"what it is waiting on.", ReadyCondition),
		"Inspect sandbox", &catalog.ToolAnnotations{ReadOnlyHint: new(true), OpenWorldHint: new(false)}))
	if err != nil {
		return Inputs{}, err
	}
	dispose, err := catalog.NewInput[NameArgs](nameOptions(Dispose,
		"Delete one sandbox of yours, and everything in it. Disposing an absent one is not an error.",
		"Dispose sandbox", &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(true), IdempotentHint: new(true)}))
	if err != nil {
		return Inputs{}, err
	}
	return Inputs{
		Create: create, GetTemplate: getTemplate, Exec: exec, DecodeExec: decodeExec, List: list, Get: get, Dispose: dispose,
	}, nil
}

func nameOptions(action Action, description, title string, annotations *catalog.ToolAnnotations) catalog.InputOptions {
	return catalog.InputOptions{
		Name:        string(action),
		Description: description,
		Title:       &title,
		Annotations: annotations,
		Configure: func(schema *jsonschema.Schema) error {
			schema.Description = nameArgsDescription
			return nil
		},
	}
}

func newExecInput(timeoutMax, outputMax int) (catalog.Input[ExecArgs], error) {
	return catalog.NewInput[ExecArgs](catalog.InputOptions{
		Name: string(Exec),
		Description: "Run one bounded Bash script in a ready sandbox of yours. A nonzero exit is a normal result, " +
			"not a failure.",
		Title:       new("Run script in sandbox"),
		Annotations: &catalog.ToolAnnotations{ReadOnlyHint: new(false), DestructiveHint: new(true), IdempotentHint: new(false)},
		Configure: func(schema *jsonschema.Schema) error {
			for _, cap := range []struct {
				name    string
				maximum int
			}{
				{name: "timeout_seconds", maximum: timeoutMax},
				{name: "max_output_bytes", maximum: outputMax},
			} {
				property, ok := schema.Properties.Get(cap.name)
				if !ok {
					return fmt.Errorf("input property %q is missing", cap.name)
				}
				property.Maximum = json.Number(strconv.Itoa(cap.maximum))
			}
			return nil
		},
	})
}

// Definitions are the Actions this contract offers, with the bound schemas intended for admission.
func (inputs Inputs) Definitions() map[string]catalog.ActionDefinition {
	definitions := make(map[string]catalog.ActionDefinition, len(inputs.actionInputs()))
	for _, input := range inputs.actionInputs() {
		definitions[input.Name()] = input.Definition()
	}
	return definitions
}
