package mcpexec

import (
	"bytes"
	"cmp"
	"encoding/json"
	"fmt"
	"log/slog"
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

// invalidCatalogError is a tool list the group cannot offer: a tool whose input schema is not a JSON Schema, a
// name listed twice, or a definition the catalog cannot hold.
type invalidCatalogError struct{ reason string }

func (e *invalidCatalogError) Error() string { return e.reason }

// mirror reads the server's tools as the group's Actions, as the Python service does. A tool whose name is not a key
// is left out; any other tool the catalog cannot hold makes the whole list invalid.
func mirror(listed []*mcp.Tool) (map[string]tool, error) {
	tools := map[string]tool{}
	for _, listed := range listed {
		if !catalog.IsKey(listed.Name) {
			slog.Warn("MCP tool name does not fit the catalog key pattern; skipping")
			continue
		}
		schema, err := json.Marshal(listed.InputSchema)
		if err != nil {
			return nil, &invalidCatalogError{fmt.Sprintf("tool %q input schema: %v", listed.Name, err)}
		}
		arguments, err := compileSchema(schema)
		if err != nil {
			return nil, &invalidCatalogError{fmt.Sprintf("tool %q input schema: %v", listed.Name, err)}
		}
		if _, listedTwice := tools[listed.Name]; listedTwice {
			return nil, &invalidCatalogError{fmt.Sprintf("tool %q: duplicate MCP tool name", listed.Name)}
		}
		description := cmp.Or(listed.Description, "MCP tool "+listed.Name)
		if utf8.RuneCountInString(description) > maxDescription {
			return nil, &invalidCatalogError{fmt.Sprintf("tool %q: description is longer than %d characters", listed.Name, maxDescription)}
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
		tools[listed.Name] = tool{definition: definition, arguments: arguments}
	}
	return tools, nil
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
