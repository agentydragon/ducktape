package centralsampler

import (
	"cmp"
	_ "embed"
	"encoding/json"
	"fmt"
	"slices"
	"strconv"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	"github.com/invopop/jsonschema"
)

// The embedded codex/*.txt files are verbatim copies of Codex rust-v0.162.1
// code-mode-protocol/src/description.rs constants (one trailing newline added):
// WAIT_DESCRIPTION_TEMPLATE and LEGACY_IMAGE_HELPER_DESCRIPTION.
var (
	//go:embed exec_description.txt
	execDescriptionTemplate string
	//go:embed codex/wait_description.txt
	waitDescriptionTemplate string
	//go:embed codex/legacy_image_helper.txt
	legacyImageHelperDescription string
)

// Codex rust-v0.162.1 core/src/tools/code_mode/execute_spec.rs CODE_MODE_FREEFORM_GRAMMAR.
const execFreeformGrammar = `
start: pragma_source | plain_source
pragma_source: PRAGMA_LINE NEWLINE SOURCE
plain_source: SOURCE

PRAGMA_LINE: /[ \t]*\/\/ @exec:[^\r\n]*/
NEWLINE: /\r?\n/
SOURCE: /[\s\S]+/
`

const (
	// Codex rust-v0.162.1 core/src/config/mod.rs DEFAULT_CODE_MODE_EXEC_YIELD_TIME_MS: core fills
	// it in when the pragma omits yield_time_ms, and the exec description advertises it.
	defaultExecYieldTimeMS = 30_000
	// Codex rust-v0.162.1 code-mode-protocol/src/runtime.rs DEFAULT_WAIT_YIELD_TIME_MS.
	defaultWaitYieldTimeMS = 10_000
	// Codex rust-v0.162.1 core/src/unified_exec/mod.rs DEFAULT_MAX_OUTPUT_TOKENS, the
	// resolve_max_tokens fallback for both exec max_output_tokens and wait max_tokens.
	defaultCodeModeMaxOutputTokens = 10_000
	// Codex rust-v0.162.1 code-mode-protocol/src/json_schema_types.rs DEFAULT_INPUT_SCHEMA_MAX_BYTES.
	defaultInputSchemaMaxBytes = 16_000
	// Codex rust-v0.162.1 code-mode-protocol/src/description.rs MAX_JS_SAFE_INTEGER.
	maxJSSafeInteger = 1<<53 - 1
	execPragmaPrefix = "// @exec:"
)

func templateText(text string) string { return strings.TrimSuffix(text, "\n") }

// execToolDescription renders Codex rust-v0.162.1 code-mode-protocol/src/description.rs
// build_exec_tool_description as core/src/tools/spec_plan.rs calls it for this sampler: Code
// Mode Only with freeform exec, the default
// `unified_image_budget = false` (legacy image helper) and no model message overrides.
// The sampler-owned exec template adapts the upstream description to the selected contract.
func execToolDescription(definitions []protocol.CodeToolDefinition, encoding protocol.ExecEncoding) (string, error) {
	inputContract := "- Accepts raw JavaScript source text, not JSON, quoted strings, or markdown code fences.\n" +
		"- You may optionally start the tool input with a first-line pragma like `// @exec: {\"yield_time_ms\": 10000, \"max_output_tokens\": 1000}`."
	if encoding == protocol.ExecEncodingJSON {
		inputContract = "- Pass JavaScript source in the JSON code field.\n" +
			"- To resume a yielded cell, pass cell_id instead of code, with optional yield_time_ms (default 10000), max_output_tokens (default 10000), and terminate: true.\n" +
			"- Resume only after exec reports Script running with cell ID. Each resume returns new output or completion.\n" +
			"- Pass exactly one of code or cell_id; terminate is only valid with cell_id."
	}
	description := strings.NewReplacer(
		"{{ default_exec_yield_time_ms }}", strconv.Itoa(defaultExecYieldTimeMS),
		"{{ image_helper }}", templateText(legacyImageHelperDescription),
		"{{ input_contract }}", inputContract,
	).Replace(templateText(execDescriptionTemplate))
	// TODO: Codex also renders DEFERRED_NESTED_TOOLS_GUIDANCE (plus TOOL_SEARCH_GUIDANCE with
	// ranked search) and the "Shared MCP Types" MCP_TYPESCRIPT_PREAMBLE section here, even with no
	// such tools. Add them back as upstream renders them once MCP/Action tools and tool
	// search/deferred tools exist in the sampler.
	sections := []string{description}
	// Codex rust-v0.162.1 core/src/tools/spec_plan.rs compare_code_mode_tools without namespaces.
	sorted := slices.Clone(definitions)
	slices.SortFunc(sorted, func(left, right protocol.CodeToolDefinition) int {
		return cmp.Or(strings.Compare(left.ToolName.Name, right.ToolName.Name), strings.Compare(left.Name, right.Name))
	})
	var nested []string
	for _, definition := range sorted {
		sample, err := codeModeSample(definition)
		if err != nil {
			return "", err
		}
		global := normalizeCodeModeIdentifier(definition.Name)
		heading := toolHeading(global, definition.Name)
		if sample = strings.TrimSpace(sample); sample == "" {
			nested = append(nested, heading)
		} else {
			nested = append(nested, heading+"\n"+sample)
		}
	}
	if len(nested) > 0 {
		sections = append(sections, strings.Join(nested, "\n\n"))
	}
	return strings.Join(sections, "\n\n"), nil
}

func jsonExecToolParameters() (json.RawMessage, error) {
	reflector := &jsonschema.Reflector{Anonymous: true, ExpandedStruct: true}
	return json.Marshal(reflector.Reflect(jsonExecInput{}))
}

// Codex rust-v0.162.1 core/src/tools/code_mode/wait_spec.rs create_wait_tool.
func waitToolDescription() string {
	return "Waits on a yielded `exec` cell and returns new output or completion.\n" + strings.TrimSpace(waitDescriptionTemplate)
}

// Codex rust-v0.162.1 core/src/tools/code_mode/wait_spec.rs create_wait_tool parameters.
func waitToolParameters() json.RawMessage {
	return json.RawMessage(`{"type":"object","properties":{` +
		`"cell_id":{"type":"string","description":"Identifier of the running exec cell."},` +
		`"max_tokens":{"type":"number","description":"Output token budget for this wait call. Defaults to 10000 tokens."},` +
		`"terminate":{"type":"boolean","description":"True stops the running exec cell; false or omitted waits for output."},` +
		`"yield_time_ms":{"type":"number","description":"Wait before yielding more output. Defaults to 10000 ms."}` +
		`},"required":["cell_id"],"additionalProperties":false}`)
}

// hostToolDefinitions mirrors Codex rust-v0.162.1 core/src/tools/code_mode/tool_definitions.rs
// prepare_code_mode_tool_definitions: the host receives each description augmented with its
// TypeScript declaration (exposed through ALL_TOOLS) and no schemas.
func hostToolDefinitions(definitions []protocol.CodeToolDefinition) ([]protocol.CodeToolDefinition, error) {
	result := make([]protocol.CodeToolDefinition, 0, len(definitions))
	for _, definition := range definitions {
		sample, err := codeModeSample(definition)
		if err != nil {
			return nil, err
		}
		definition.Description = sample
		definition.InputSchema = nil
		definition.OutputSchema = nil
		result = append(result, definition)
	}
	slices.SortStableFunc(result, func(left, right protocol.CodeToolDefinition) int { return strings.Compare(left.Name, right.Name) })
	return result, nil
}

func codeModeSample(definition protocol.CodeToolDefinition) (string, error) {
	var input, output any
	if definition.InputSchema != nil {
		var err error
		if input, err = schemaValue(definition.InputSchema); err != nil {
			return "", err
		}
	}
	if definition.OutputSchema != nil {
		var err error
		if output, err = schemaValue(definition.OutputSchema); err != nil {
			return "", err
		}
	}
	return renderCodeModeSample(definition.Name, definition.Description, definition.Kind, input, output)
}

// renderCodeModeSample ports Codex rust-v0.162.1 code-mode-protocol/src/description.rs
// render_code_mode_sample_for_definition; a nil schema is absent. MCP CallToolResult output
// detection is omitted: no nested tool here is an MCP tool.
func renderCodeModeSample(name, description, kind string, input, output any) (string, error) {
	inputName, inputType := "input", "string"
	if kind == "function" {
		inputName, inputType = "args", "unknown"
		if input != nil {
			inputType = renderJSONSchemaTypeScript(input)
		}
	} else if kind != "freeform" {
		return "", fmt.Errorf("unsupported code-mode tool kind %q", kind)
	}
	outputType := "unknown"
	if output != nil {
		outputType = renderJSONSchemaTypeScript(output)
	}
	declaration := fmt.Sprintf("declare const tools: { %s(%s: %s): Promise<%s>; };",
		normalizeCodeModeIdentifier(name), inputName, inputType, outputType)
	return description + "\n\nexec tool declaration:\n```ts\n" + declaration + "\n```", nil
}

func schemaValue(schema any) (any, error) {
	encoded, err := json.Marshal(schema)
	if err != nil {
		return nil, err
	}
	var value any
	if err := json.Unmarshal(encoded, &value); err != nil {
		return nil, err
	}
	return value, nil
}

// Codex rust-v0.162.1 code-mode-protocol/src/description.rs render_tool_heading.
func toolHeading(global, raw string) string {
	if global == raw {
		return "### `" + global + "`"
	}
	return "### `" + global + "` (`" + raw + "`)"
}

// Codex rust-v0.162.1 code-mode-protocol/src/description.rs normalize_code_mode_identifier.
func normalizeCodeModeIdentifier(key string) string {
	var identifier strings.Builder
	for index, char := range key {
		valid := char == '_' || char == '$' || char >= 'a' && char <= 'z' || char >= 'A' && char <= 'Z' ||
			index > 0 && char >= '0' && char <= '9'
		if valid {
			identifier.WriteRune(char)
		} else {
			identifier.WriteByte('_')
		}
	}
	if identifier.Len() == 0 {
		return "_"
	}
	return identifier.String()
}

// renderJSONSchemaTypeScript ports Codex rust-v0.162.1
// code-mode-protocol/src/json_schema_types.rs render_json_schema_to_typescript for the keywords
// local tool schemas use. `$ref` renders as `unknown` (no local schema uses one), and the
// incremental render-work budget is reduced to its final byte cap.
func renderJSONSchemaTypeScript(schema any) string {
	rendered := renderSchema(schema)
	if len(rendered) > defaultInputSchemaMaxBytes {
		return "unknown"
	}
	return rendered
}

func renderSchema(schema any) string {
	switch value := schema.(type) {
	case bool:
		if value {
			return "unknown"
		}
		return "never"
	case map[string]any:
		return renderSchemaMap(value)
	default:
		return "unknown"
	}
}

func renderSchemaMap(schema map[string]any) string {
	if _, ok := schema["$ref"]; ok {
		return "unknown"
	}
	if value, ok := schema["const"]; ok {
		return renderSchemaLiteral(value)
	}
	if values, ok := schema["enum"].([]any); ok && len(values) > 0 {
		literals := make([]string, 0, len(values))
		for _, value := range values {
			literals = append(literals, renderSchemaLiteral(value))
		}
		return strings.Join(literals, " | ")
	}
	for _, key := range []string{"anyOf", "oneOf"} {
		if variants, ok := schema[key].([]any); ok && len(variants) > 0 {
			rendered := make([]string, 0, len(variants))
			for _, variant := range variants {
				rendered = append(rendered, renderSchema(variant))
			}
			return strings.Join(rendered, " | ")
		}
	}
	if variants, ok := schema["allOf"].([]any); ok && len(variants) > 0 {
		rendered := make([]string, 0, len(variants))
		for _, variant := range variants {
			part := renderSchema(variant)
			if strings.Contains(part, " | ") {
				part = "(" + part + ")"
			}
			rendered = append(rendered, part)
		}
		return strings.Join(rendered, " & ")
	}
	switch schemaType := schema["type"].(type) {
	case []any:
		var rendered []string
		for _, item := range schemaType {
			if name, ok := item.(string); ok {
				rendered = append(rendered, renderSchemaType(schema, name))
			}
		}
		if len(rendered) > 0 {
			return strings.Join(rendered, " | ")
		}
	case string:
		return renderSchemaType(schema, schemaType)
	}
	_, hasProperties := schema["properties"]
	_, hasAdditional := schema["additionalProperties"]
	_, hasRequired := schema["required"]
	if hasProperties || hasAdditional || hasRequired {
		return renderSchemaObject(schema)
	}
	_, hasItems := schema["items"]
	_, hasPrefixItems := schema["prefixItems"]
	if hasItems || hasPrefixItems {
		return renderSchemaArray(schema)
	}
	return "unknown"
}

func renderSchemaType(schema map[string]any, schemaType string) string {
	switch schemaType {
	case "string", "boolean", "null":
		return schemaType
	case "number", "integer":
		return "number"
	case "array":
		return renderSchemaArray(schema)
	case "object":
		return renderSchemaObject(schema)
	default:
		return "unknown"
	}
}

func renderSchemaArray(schema map[string]any) string {
	if items, ok := schema["items"]; ok {
		return "Array<" + renderSchema(items) + ">"
	}
	if items, ok := schema["prefixItems"].([]any); ok && len(items) > 0 {
		rendered := make([]string, 0, len(items))
		for _, item := range items {
			rendered = append(rendered, renderSchema(item))
		}
		return "[" + strings.Join(rendered, ", ") + "]"
	}
	return "unknown[]"
}

func renderSchemaObject(schema map[string]any) string {
	required := map[string]bool{}
	if names, ok := schema["required"].([]any); ok {
		for _, name := range names {
			if name, ok := name.(string); ok {
				required[name] = true
			}
		}
	}
	properties, _ := schema["properties"].(map[string]any)
	names := make([]string, 0, len(properties))
	described := false
	for name, value := range properties {
		names = append(names, name)
		if property, ok := value.(map[string]any); ok {
			if description, ok := property["description"].(string); ok && description != "" {
				described = true
			}
		}
	}
	slices.Sort(names)
	property := func(name string) string {
		optional := "?"
		if required[name] {
			optional = ""
		}
		propertyName := name
		if normalizeCodeModeIdentifier(name) != name {
			propertyName = renderSchemaLiteral(name)
		}
		return propertyName + optional + ": " + renderSchema(properties[name]) + ";"
	}
	var additional *string
	if value, ok := schema["additionalProperties"]; ok {
		switch value {
		case true:
			line := "[key: string]: unknown;"
			additional = &line
		case false:
		default:
			line := "[key: string]: " + renderSchema(value) + ";"
			additional = &line
		}
	} else if len(properties) == 0 {
		line := "[key: string]: unknown;"
		additional = &line
	}
	if described {
		lines := []string{"{"}
		for _, name := range names {
			if property, ok := properties[name].(map[string]any); ok {
				if description, ok := property["description"].(string); ok {
					for _, line := range strings.Split(description, "\n") {
						if line = strings.TrimSpace(line); line != "" {
							lines = append(lines, "  // "+line)
						}
					}
				}
			}
			lines = append(lines, "  "+property(name))
		}
		if additional != nil {
			lines = append(lines, "  "+*additional)
		}
		return strings.Join(append(lines, "}"), "\n")
	}
	var lines []string
	for _, name := range names {
		lines = append(lines, property(name))
	}
	if additional != nil {
		lines = append(lines, *additional)
	}
	if len(lines) == 0 {
		return "{}"
	}
	return "{ " + strings.Join(lines, " ") + " }"
}

func renderSchemaLiteral(value any) string {
	encoded, err := json.Marshal(value)
	if err != nil {
		return "unknown"
	}
	return string(encoded)
}
