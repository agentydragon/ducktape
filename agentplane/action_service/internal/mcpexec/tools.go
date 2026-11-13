package mcpexec

import (
	"bytes"
	"cmp"
	"encoding/json"
	"fmt"
	"log/slog"
	"slices"
	"strings"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/santhosh-tekuri/jsonschema/v6"
)

// maxDescription is the longest Action description the catalog holds; some MCP servers write long ones.
const maxDescription = 20_000

// tool is one tool the server offers, as an Action of the group.
type tool struct {
	definition catalog.ActionDefinition
	// arguments is definition's input schema, compiled.
	arguments *jsonschema.Schema
}

// invalidCatalogError is a tool list the group cannot offer: every tool in it is one the catalog cannot hold.
type invalidCatalogError struct{ reason string }

func (e *invalidCatalogError) Error() string { return e.reason }

// skipped maps the name of each listed tool the catalog cannot hold to why: an input schema that is not a JSON
// Schema, a name listed twice, or a definition the catalog cannot hold. Such a tool is left out; the others are
// still offered.
type skipped map[string]string

func (s skipped) String() string {
	names := make([]string, 0, len(s))
	for name := range s {
		names = append(names, name)
	}
	slices.Sort(names)
	parts := make([]string, len(names))
	for i, name := range names {
		parts[i] = fmt.Sprintf("tool %q: %s", name, s[name])
	}
	return strings.Join(parts, "; ")
}

// mirror reads the server's tools as the group's Actions. A tool whose name is not a key is left out silently. A tool
// the catalog cannot hold is left out and reported in skipped, so one bad tool does not take the others with it; the
// list is invalid only when every tool in it is skipped.
func mirror(listed []*mcp.Tool) (map[string]tool, skipped, error) {
	tools := map[string]tool{}
	skip := skipped{}
	for _, listed := range listed {
		if !catalog.IsKey(listed.Name) {
			slog.Warn("MCP tool name does not fit the catalog key pattern; skipping")
			continue
		}
		if _, listedTwice := tools[listed.Name]; listedTwice {
			// Which of the two is meant is unknowable, so neither is offered.
			delete(tools, listed.Name)
			skip[listed.Name] = "duplicate MCP tool name"
			continue
		}
		if _, dropped := skip[listed.Name]; dropped {
			continue
		}
		definition, arguments, err := mirrorOne(listed)
		if err != nil {
			skip[listed.Name] = err.Error()
			continue
		}
		tools[listed.Name] = tool{definition: definition, arguments: arguments}
	}
	if len(tools) == 0 && len(skip) > 0 {
		return nil, skip, &invalidCatalogError{skip.String()}
	}
	return tools, skip, nil
}

// mirrorOne is one listed tool as an ActionDefinition and its compiled input schema.
func mirrorOne(listed *mcp.Tool) (catalog.ActionDefinition, *jsonschema.Schema, error) {
	schema, err := json.Marshal(listed.InputSchema)
	if err != nil {
		return catalog.ActionDefinition{}, nil, fmt.Errorf("input schema: %w", err)
	}
	arguments, err := compileSchema(schema)
	if err != nil {
		return catalog.ActionDefinition{}, nil, fmt.Errorf("input schema: %w", err)
	}
	description := cmp.Or(listed.Description, "MCP tool "+listed.Name)
	if utf8.RuneCountInString(description) > maxDescription {
		return catalog.ActionDefinition{}, nil, fmt.Errorf("description is longer than %d characters", maxDescription)
	}
	definition := catalog.ActionDefinition{Description: description, InputSchema: schema}
	title := listed.Title
	if hints := listed.Annotations; hints != nil {
		// `annotations.title` is the older spelling of the display name that `title` replaced.
		title = cmp.Or(title, hints.Title)
		definition.Annotations = &catalog.ToolAnnotations{
			Title:           optional(hints.Title),
			ReadOnlyHint:    optional(hints.ReadOnlyHint),
			DestructiveHint: hints.DestructiveHint,
			IdempotentHint:  optional(hints.IdempotentHint),
			OpenWorldHint:   hints.OpenWorldHint,
		}
	}
	definition.Title = optional(title)
	return definition, arguments, nil
}

// optional is v, or nil for its zero value: go-sdk reads an absent title as "" and an absent read-only or idempotent
// hint as false, which the protocol defines it to mean.
func optional[T comparable](v T) *T {
	var zero T
	if v == zero {
		return nil
	}
	return &v
}

// compileSchema compiles an input schema, which must be a JSON object, as catalog.CompileSchema does.
func compileSchema(schema json.RawMessage) (*jsonschema.Schema, error) {
	document, err := jsonschema.UnmarshalJSON(bytes.NewReader(schema))
	if err != nil {
		return nil, err
	}
	if _, isObject := document.(map[string]any); !isObject {
		return nil, fmt.Errorf("must be a JSON object")
	}
	return catalog.CompileSchema(document)
}

// matches is whether arguments, a JSON object, satisfy the tool's input schema.
func (t tool) matches(arguments json.RawMessage) bool {
	value, err := jsonschema.UnmarshalJSON(bytes.NewReader(arguments))
	return err == nil && t.arguments.Validate(value) == nil
}

// storedResult is the tool's answer as its Execution keeps it: the CallToolResult whole, every content block,
// structured content, `isError` and `_meta`, so that it can be relayed unchanged. The result's type describes the
// exchange, not the answer, and is left out.
func storedResult(result *mcp.CallToolResult) (json.RawMessage, error) {
	content := result.Content
	if content == nil {
		content = []mcp.Content{}
	}
	return json.Marshal(&mcp.CallToolResult{
		Meta: result.Meta, Content: content, StructuredContent: result.StructuredContent, IsError: result.IsError,
	})
}

// reportsUnknownOutcome is whether an error result says, in the bounded machine-readable form a backend uses for it,
// that the backend cannot tell whether the call took effect: a text block holding a JSON object whose `kind` is
// `execution_unknown`. The first text block with a string `kind` decides.
func reportsUnknownOutcome(result *mcp.CallToolResult) bool {
	if !result.IsError {
		return false
	}
	for _, block := range result.Content {
		text, isText := block.(*mcp.TextContent)
		if !isText {
			continue
		}
		var payload map[string]any
		if json.Unmarshal([]byte(text.Text), &payload) != nil {
			continue
		}
		if kind, isString := payload["kind"].(string); isString {
			return kind == "execution_unknown"
		}
	}
	return false
}
