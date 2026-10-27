// Package catalog is the Action Service's reviewed ActionGroups: the `action_groups` setting, each group bound to the
// executor its Actions run on, with the Actions it offers. The same lookup routes an Execution to its executor.
package catalog

import (
	"cmp"
	"encoding/json"
	"errors"
	"fmt"
	"iter"
	"maps"
	"math"
	"regexp"
	"slices"
	"strings"
	"sync"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"gopkg.in/yaml.v3"
)

// keyPattern is what a group or Action key matches: a stable lowercase identifier.
var keyPattern = regexp.MustCompile(`^[a-z][a-z0-9_-]*$`)

const maxKeyLength = 200

// DirectToolSeparator joins a group's key and an Action's into the name of a direct tool, which is split at the first
// one.
const DirectToolSeparator = "__"

// ActionDefinition is one Action's description and parameter contract.
type ActionDefinition struct {
	Description string
	// InputSchema is the JSON Schema a submission's arguments are validated against: a JSON object.
	InputSchema json.RawMessage
	// Title is the Action's display name; nil when it has none.
	Title *string
	// Annotations are the MCP tool's behavior hints; nil when it gives none. They shape how a client presents the
	// Action as a direct tool, never what the service allows.
	Annotations *ToolAnnotations
}

// ToolAnnotations are an MCP tool's behavior hints. Each is nil where the tool does not give it.
type ToolAnnotations struct {
	Title           *string
	ReadOnlyHint    *bool
	DestructiveHint *bool
	IdempotentHint  *bool
	OpenWorldHint   *bool
}

// Binding is where a group's Actions run: an MCPBinding or a PodBinding.
type Binding interface{ isBinding() }

// MCPBinding reaches a reviewed upstream MCP server.
type MCPBinding struct {
	// Description is the executor as a caller sees it, such as whose account it acts as.
	Description string
	// Config is the MCP server's configuration, a JSON object the MCP executor reads. No caller sees it.
	Config json.RawMessage
}

// PodBinding runs the sandbox Actions in this service, as their caller, in boxes stamped from PodTemplates.
type PodBinding struct {
	// Description is the executor as a caller sees it.
	Description string
	// Namespace is where boxes run and are exec'd.
	Namespace string
	// Templates are the boxes a caller may create: each names a PodTemplate in Namespace and says what a box made
	// from it holds, for the caller choosing one.
	Templates         map[string]string
	MaxTimeoutSeconds int
	MaxOutputBytes    int
}

func (MCPBinding) isBinding() {}
func (PodBinding) isBinding() {}

// Group is the unit of discovery and ownership: one executor binding, many Actions.
type Group struct {
	Title       string
	Description string
	Executor    Binding
	// Available is whether the group is offered now.
	Available bool
	Actions   map[string]ActionDefinition
	// DirectTools are the Actions a Connection also sees as MCP tools of its own, named
	// `<group>__<action>`; nil when the group offers none.
	DirectTools map[string]struct{}
	// Health is this replica's view of an MCP group's connection, as its executor reports it; nil for a group without
	// one.
	Health *action_servicepb.McpHealth
}

// Catalog is the groups this process was started with, by key. The zero Catalog holds none.
//
// While the catalog serves, a group changes only through Update, which replaces it: a group read from the catalog
// never changes under its reader.
type Catalog struct {
	// mu guards which group each key holds, and changed.
	mu     sync.RWMutex
	groups map[string]*Group
	// changed is closed by the next Update; nil in the zero Catalog, which never changes.
	changed chan struct{}
}

// Groups are the catalog's groups in key order, as they stood when the iteration began.
func (c *Catalog) Groups() iter.Seq2[string, *Group] {
	return func(yield func(string, *Group) bool) {
		c.mu.RLock()
		keys := slices.Sorted(maps.Keys(c.groups))
		groups := make([]*Group, len(keys))
		for i, key := range keys {
			groups[i] = c.groups[key]
		}
		c.mu.RUnlock()
		for i, key := range keys {
			if !yield(key, groups[i]) {
				return
			}
		}
	}
}

// Changed is closed by the next Update. A reader takes it before it reads the groups, so that a change made while it
// reads still wakes it.
func (c *Catalog) Changed() <-chan struct{} {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.changed
}

// Update changes the group under key while the catalog serves, as the group's executor learns its backend's Actions
// and health, and wakes the readers waiting on Changed. change gets a copy of the group, must not read the catalog,
// and replaces the Actions or health it changes rather than modifying them, as a reader may still hold the group as
// it was. A key the catalog does not hold panics: an executor serves one of the catalog's groups.
func (c *Catalog) Update(key string, change func(*Group)) {
	c.mu.Lock()
	defer c.mu.Unlock()
	current, held := c.groups[key]
	if !held {
		panic(fmt.Sprintf("catalog: no ActionGroup %q to update", key))
	}
	next := *current
	change(&next)
	c.groups[key] = &next
	close(c.changed)
	c.changed = make(chan struct{})
}

var (
	// ErrUnknownAction is an Action the catalog does not hold.
	ErrUnknownAction = errors.New("unknown Action")
	// ErrUnavailable is an Action of a group that is not offered now.
	ErrUnavailable = errors.New("ActionGroup is temporarily unavailable")
)

// Resolve returns the Action and its group. An unavailable group's Actions are ErrUnavailable whether or not it
// lists them, as it lists none while it is unavailable.
func (c *Catalog) Resolve(group, action string) (*Group, ActionDefinition, error) {
	c.mu.RLock()
	g, known := c.groups[group]
	c.mu.RUnlock()
	if known && !g.Available {
		return nil, ActionDefinition{}, ErrUnavailable
	}
	var definition ActionDefinition
	if known {
		definition, known = g.Actions[action]
	}
	if !known {
		return nil, ActionDefinition{}, fmt.Errorf("%w %s/%s", ErrUnknownAction, group, action)
	}
	return g, definition, nil
}

// Settings is the `action_groups` setting, the groups by key, as gopkg.in/yaml.v3 decodes it. Decode it with
// KnownFields(true), so that a key no field names is refused as the Python service refuses it, then build the
// Catalog with New.
type Settings map[string]groupSettings

type groupSettings struct {
	Title       string                    `yaml:"title"`
	Description string                    `yaml:"description"`
	Executor    *executorSettings         `yaml:"executor"`
	Available   *bool                     `yaml:"available"`
	Actions     map[string]actionSettings `yaml:"actions"`
	DirectTools []string                  `yaml:"direct_tools"`
}

// executorSettings is an executor's settings, decoded as the kind its `kind` names.
type executorSettings struct{ kind executorKind }

// executorKind is one executor kind's settings: an *mcpExecutorSettings or a *podExecutorSettings.
type executorKind interface {
	binding() (Binding, error)
	// unknownKeys are the keys the kind has no field for, `kind` among them. UnmarshalYAML refuses them itself:
	// Node.Decode, which it decodes the kind with, ignores KnownFields (https://github.com/go-yaml/yaml/issues/460).
	unknownKeys() map[string]yaml.Node
}

type mcpExecutorSettings struct {
	Description string               `yaml:"description"`
	Config      map[string]any       `yaml:"config"`
	Unknown     map[string]yaml.Node `yaml:",inline"`
}

type podExecutorSettings struct {
	Description string            `yaml:"description"`
	Namespace   string            `yaml:"namespace"`
	Templates   map[string]string `yaml:"templates"`
	// The limits decode as float64, as an int field would take 60.5 as 60.
	MaxTimeoutSeconds *float64             `yaml:"max_timeout_seconds"`
	MaxOutputBytes    *float64             `yaml:"max_output_bytes"`
	Unknown           map[string]yaml.Node `yaml:",inline"`
}

func (s *mcpExecutorSettings) unknownKeys() map[string]yaml.Node { return s.Unknown }
func (s *podExecutorSettings) unknownKeys() map[string]yaml.Node { return s.Unknown }

// UnmarshalYAML decodes node as the executor kind its `kind` names, refusing a key that kind has no field for. Its
// errors are yaml.TypeErrors, which the decoder reports with its own.
func (s *executorSettings) UnmarshalYAML(node *yaml.Node) error {
	var discriminator struct {
		Kind string `yaml:"kind"`
	}
	if err := node.Decode(&discriminator); err != nil {
		return err
	}
	switch discriminator.Kind {
	case "mcp":
		s.kind = &mcpExecutorSettings{}
	case "pod":
		s.kind = &podExecutorSettings{}
	default:
		return &yaml.TypeError{Errors: []string{fmt.Sprintf("line %d: executor kind must be mcp or pod", node.Line)}}
	}
	if err := node.Decode(s.kind); err != nil {
		return err
	}
	unknown := s.kind.unknownKeys()
	delete(unknown, "kind")
	if len(unknown) == 0 {
		return nil
	}
	var problems []string
	for _, key := range slices.Sorted(maps.Keys(unknown)) {
		problems = append(problems,
			fmt.Sprintf("line %d: field %s not found in an executor of kind %s", unknown[key].Line, key, discriminator.Kind))
	}
	return &yaml.TypeError{Errors: problems}
}

type actionSettings struct {
	Description string              `yaml:"description"`
	InputSchema map[string]any      `yaml:"input_schema"`
	Title       *string             `yaml:"title"`
	Annotations *annotationSettings `yaml:"annotations"`
}

// annotationSettings are an MCP tool's hints as the Python MCP SDK's model reads them: by the protocol's camel-case
// name or by the model's own field name, the former first.
type annotationSettings struct {
	Title               *string `yaml:"title"`
	ReadOnlyHint        *bool   `yaml:"readOnlyHint"`
	ReadOnlyHintName    *bool   `yaml:"read_only_hint"`
	DestructiveHint     *bool   `yaml:"destructiveHint"`
	DestructiveHintName *bool   `yaml:"destructive_hint"`
	IdempotentHint      *bool   `yaml:"idempotentHint"`
	IdempotentHintName  *bool   `yaml:"idempotent_hint"`
	OpenWorldHint       *bool   `yaml:"openWorldHint"`
	OpenWorldHintName   *bool   `yaml:"open_world_hint"`
	// Ignored takes the keys the model ignores, which KnownFields would refuse.
	Ignored map[string]any `yaml:",inline"`
}

// New builds the catalog the settings describe, refusing what the Python service refuses but for a group's `health`,
// which is the MCP executor's report and not configuration: Settings has no field for it.
//
// Its errors name keys and fields, never a value: an MCP server's configuration can hold a credential.
func New(settings Settings) (*Catalog, error) {
	c := &Catalog{groups: make(map[string]*Group, len(settings)), changed: make(chan struct{})}
	for _, key := range slices.Sorted(maps.Keys(settings)) {
		if !IsKey(key) {
			return nil, fmt.Errorf("group key %q is not a lowercase identifier of at most %d characters", key, maxKeyLength)
		}
		group, err := newGroup(key, settings[key])
		if err != nil {
			return nil, fmt.Errorf("group %s: %w", key, err)
		}
		c.groups[key] = group
	}
	return c, nil
}

func newGroup(key string, s groupSettings) (*Group, error) {
	if err := checkLength("title", s.Title, 1, 200); err != nil {
		return nil, err
	}
	if err := checkLength("description", s.Description, 1, 2000); err != nil {
		return nil, err
	}
	if s.Executor == nil {
		return nil, errors.New("executor is required")
	}
	executor, err := s.Executor.kind.binding()
	if err != nil {
		return nil, fmt.Errorf("executor: %w", err)
	}
	// The operator settings page joins a group to its MCP server's linkage by this key.
	if mcp, ok := s.Executor.kind.(*mcpExecutorSettings); ok {
		if serverID, set := mcp.Config["server_id"]; set && serverID != nil {
			if id, ok := serverID.(string); !ok || id != key {
				return nil, errors.New("executor.config.server_id must be the group's key")
			}
		}
	}
	g := &Group{
		Title:       s.Title,
		Description: s.Description,
		Executor:    executor,
		Available:   s.Available == nil || *s.Available,
		Actions:     make(map[string]ActionDefinition, len(s.Actions)),
	}
	for _, name := range slices.Sorted(maps.Keys(s.Actions)) {
		if !IsKey(name) {
			return nil, fmt.Errorf("Action key %q is not a lowercase identifier of at most %d characters", name, maxKeyLength)
		}
		if g.Actions[name], err = newAction(s.Actions[name]); err != nil {
			return nil, fmt.Errorf("Action %s: %w", name, err)
		}
	}
	if s.DirectTools == nil {
		return g, nil
	}
	if len(s.DirectTools) == 0 {
		return nil, errors.New("direct_tools must name an Action when it is set")
	}
	if strings.Contains(key, DirectToolSeparator) {
		return nil, fmt.Errorf("offers direct tools, so its key must not contain %q: a direct tool's name is split at the first one",
			DirectToolSeparator)
	}
	g.DirectTools = make(map[string]struct{}, len(s.DirectTools))
	for _, tool := range s.DirectTools {
		if !IsKey(tool) {
			return nil, errors.New("direct_tools must be a list of Action keys")
		}
		g.DirectTools[tool] = struct{}{}
	}
	return g, nil
}

func (s *mcpExecutorSettings) binding() (Binding, error) {
	if err := checkLength("description", s.Description, 1, 2000); err != nil {
		return nil, err
	}
	config, err := jsonObject(s.Config)
	if err != nil {
		return nil, fmt.Errorf("config %w", err)
	}
	return MCPBinding{Description: s.Description, Config: config}, nil
}

func (s *podExecutorSettings) binding() (Binding, error) {
	if err := checkLength("description", s.Description, 1, 2000); err != nil {
		return nil, err
	}
	if s.Namespace == "" {
		return nil, errors.New("namespace is required")
	}
	if len(s.Templates) == 0 {
		return nil, errors.New("templates must describe at least one PodTemplate by name")
	}
	timeout, err := limit("max_timeout_seconds", s.MaxTimeoutSeconds, 1800, 1, 3600)
	if err != nil {
		return nil, err
	}
	output, err := limit("max_output_bytes", s.MaxOutputBytes, 200_000, 0, 1_000_000)
	if err != nil {
		return nil, err
	}
	return PodBinding{
		Description:       s.Description,
		Namespace:         s.Namespace,
		Templates:         s.Templates,
		MaxTimeoutSeconds: timeout,
		MaxOutputBytes:    output,
	}, nil
}

func newAction(s actionSettings) (ActionDefinition, error) {
	if err := checkLength("description", s.Description, 1, 20_000); err != nil {
		return ActionDefinition{}, err
	}
	schema, err := jsonObject(s.InputSchema)
	if err != nil {
		return ActionDefinition{}, fmt.Errorf("input_schema %w", err)
	}
	action := ActionDefinition{Description: s.Description, InputSchema: schema, Title: s.Title}
	if a := s.Annotations; a != nil {
		action.Annotations = &ToolAnnotations{
			Title:           a.Title,
			ReadOnlyHint:    cmp.Or(a.ReadOnlyHint, a.ReadOnlyHintName),
			DestructiveHint: cmp.Or(a.DestructiveHint, a.DestructiveHintName),
			IdempotentHint:  cmp.Or(a.IdempotentHint, a.IdempotentHintName),
			OpenWorldHint:   cmp.Or(a.OpenWorldHint, a.OpenWorldHintName),
		}
	}
	return action, nil
}

// jsonObject is a decoded YAML mapping as JSON; an empty object when it is absent.
func jsonObject(value map[string]any) (json.RawMessage, error) {
	if value == nil {
		return json.RawMessage(`{}`), nil
	}
	document, err := json.Marshal(value)
	if err != nil {
		// Such as a nested mapping whose keys are not strings.
		return nil, errors.New("must be a JSON object")
	}
	return document, nil
}

// limit is value as a whole number from minimum to maximum, or fallback when it is absent.
func limit(name string, value *float64, fallback, minimum, maximum int) (int, error) {
	if value == nil {
		return fallback, nil
	}
	if *value != math.Trunc(*value) || *value < float64(minimum) || *value > float64(maximum) {
		return 0, fmt.Errorf("%s must be a whole number from %d to %d", name, minimum, maximum)
	}
	return int(*value), nil
}

func checkLength(name, value string, minimum, maximum int) error {
	if length := utf8.RuneCountInString(value); length < minimum || length > maximum {
		return fmt.Errorf("%s must be %d to %d characters", name, minimum, maximum)
	}
	return nil
}

// IsKey is whether s is a group or Action key: a lowercase identifier of at most 200 characters.
func IsKey(s string) bool {
	return utf8.RuneCountInString(s) <= maxKeyLength && keyPattern.MatchString(s)
}
