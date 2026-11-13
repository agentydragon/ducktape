package toolcontract

import (
	"encoding/json"
	"regexp"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

func TestExecDescriptionDeclaresEveryNestedToolInput(t *testing.T) {
	definitions := testToolDefinitions()
	description, err := ExecToolDescription(definitions, protocol.ExecEncodingFreeform)
	if err != nil {
		t.Fatal(err)
	}
	if len(definitions) == 0 {
		t.Fatal("exec description has no nested tool definitions")
	}
	if strings.Contains(description, "{{") {
		t.Fatalf("exec description kept a template placeholder: %s", description)
	}
	for _, definition := range definitions {
		heading := toolHeading(normalizeCodeModeIdentifier(definition.Name), definition.Name)
		sectionMarker := "\n\n" + heading + "\n"
		if count := strings.Count(description, sectionMarker); count != 1 {
			t.Fatalf("exec description has %d sections for %s, want exactly one", count, definition.Name)
		}
		sectionStart := strings.Index(description, sectionMarker) + len(sectionMarker)
		sectionEnd := strings.Index(description[sectionStart:], "\n\n### ")
		section := description[sectionStart:]
		if sectionEnd >= 0 {
			section = section[:sectionEnd]
		}
		const codeFence = "```ts\n"
		if count := strings.Count(section, codeFence); count != 1 {
			t.Fatalf("section for %s has %d TypeScript declarations, want exactly one", definition.Name, count)
		}
		declarationStart := strings.Index(section, codeFence) + len(codeFence)
		declarationEnd := strings.Index(section[declarationStart:], "\n```")
		if declarationEnd < 0 {
			t.Fatalf("section for %s has an unterminated TypeScript declaration", definition.Name)
		}
		declaration := section[declarationStart : declarationStart+declarationEnd]
		if definition.Kind == "function" {
			schema, err := schemaValue(definition.InputSchema)
			if err != nil {
				t.Fatal(err)
			}
			properties := schema.(map[string]any)["properties"].(map[string]any)
			declarationPrefix := "declare const tools: { " + normalizeCodeModeIdentifier(definition.Name) + "(args: "
			if strings.Count(declaration, declarationPrefix) != 1 {
				t.Fatalf("section for %s lacks its own tool declaration: %s", definition.Name, declaration)
			}
			argumentsStart := strings.Index(declaration, declarationPrefix) + len(declarationPrefix)
			argumentsEnd := strings.Index(declaration[argumentsStart:], "): Promise<")
			if argumentsEnd < 0 {
				t.Fatalf("section for %s has an unterminated argument declaration: %s", definition.Name, declaration)
			}
			arguments := declaration[argumentsStart : argumentsStart+argumentsEnd]
			for property := range properties {
				if !hasTypeScriptProperty(arguments, property) {
					t.Fatalf("exec description does not declare %s.%s", definition.Name, property)
				}
			}
		} else {
			declarationPrefix := "declare const tools: { " + normalizeCodeModeIdentifier(definition.Name) + "(input: string)"
			if !strings.Contains(declaration, declarationPrefix) {
				t.Fatalf("section for %s lacks its own tool declaration: %s", definition.Name, declaration)
			}
		}
	}
}

func hasTypeScriptProperty(declaration, name string) bool {
	property := name
	if normalizeCodeModeIdentifier(name) != name {
		encoded, err := json.Marshal(name)
		if err != nil {
			return false
		}
		property = string(encoded)
	}
	pattern := regexp.MustCompile(`(^|[;{\n])\s*` + regexp.QuoteMeta(property) + `[?]?:`)
	return pattern.MatchString(declaration)
}

func TestHostToolDefinitionsCarryDeclarationsWithoutSchemas(t *testing.T) {
	source := testToolDefinitions()
	if len(source) == 0 {
		t.Fatal("toolDefinitions returned no tools")
	}
	definitions, err := HostToolDefinitions(source)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(definitions)
	if err != nil {
		t.Fatal(err)
	}
	var wire []map[string]any
	if err := json.Unmarshal(encoded, &wire); err != nil {
		t.Fatal(err)
	}
	wantNames := make([]string, 0, len(source))
	for _, definition := range source {
		wantNames = append(wantNames, definition.Name)
	}
	slices.Sort(wantNames)
	gotNames := make([]string, 0, len(wire))
	for _, definition := range wire {
		name, ok := definition["name"].(string)
		if !ok {
			t.Fatalf("host definition has no string name: %v", definition)
		}
		gotNames = append(gotNames, name)
		if definition["input_schema"] != nil || definition["output_schema"] != nil {
			t.Fatalf("host definition kept a schema: %v", definition)
		}
		description, ok := definition["description"].(string)
		if !ok || !strings.Contains(description, "exec tool declaration:") {
			t.Fatalf("host definition description lacks its declaration: %v", definition)
		}
		declaration := "declare const tools: { " + normalizeCodeModeIdentifier(name) + "("
		if strings.Count(description, declaration) != 1 {
			t.Fatalf("host definition for %s has %d matching declarations, want exactly one: %s", name, strings.Count(description, declaration), description)
		}
	}
	slices.Sort(gotNames)
	if !slices.Equal(gotNames, wantNames) {
		t.Fatalf("host definition names = %v, want exactly one of each source tool name %v", gotNames, wantNames)
	}
}

func testToolDefinitions() []protocol.CodeToolDefinition {
	closed := false
	return []protocol.CodeToolDefinition{
		{
			Name: "weather-tool", ToolName: protocol.ToolName{Name: "weather-tool"},
			Description: "Look up weather.", Kind: "function",
			InputSchema: &protocol.JSONSchema{
				Type: "object", Required: []string{"city"}, AdditionalProperties: &closed,
				Properties: map[string]protocol.JSONSchema{"city": {Type: "string"}},
			},
			OutputSchema: &protocol.JSONSchema{
				Type: "object", Required: []string{"forecast"},
				Properties: map[string]protocol.JSONSchema{"forecast": {Type: "string"}},
			},
		},
		{
			Name: "apply-patch", ToolName: protocol.ToolName{Name: "apply-patch"},
			Description: "Apply a patch.", Kind: "freeform",
		},
	}
}

// Expectations come from Codex rust-v0.162.1 code-mode-protocol/src/description.rs tests
// augment_tool_definition_appends_typed_declaration and
// augment_tool_definition_includes_property_descriptions_as_comments.
func TestCodeModeSampleMatchesCodexDeclarations(t *testing.T) {
	for _, tc := range []struct {
		name, input, output, expected string
	}{
		{
			name:     "hidden_dynamic_tool",
			input:    `{"type":"object","properties":{"city":{"type":"string"}},"required":["city"],"additionalProperties":false}`,
			output:   `{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}`,
			expected: "hidden_dynamic_tool(args: { city: string; }): Promise<{ ok: boolean; }>;",
		},
		{
			name:   "weather_tool",
			input:  `{"type":"object","properties":{"weather":{"type":"array","description":"look up weather for a given list of locations","items":{"type":"object","properties":{"location":{"type":"string"}},"required":["location"]}}},"required":["weather"]}`,
			output: `{"type":"object","properties":{"forecast":{"type":"string","description":"human readable weather forecast"}},"required":["forecast"]}`,
			expected: "weather_tool(args: {\n  // look up weather for a given list of locations\n  weather: Array<{ location: string; }>;\n" +
				"}): Promise<{\n  // human readable weather forecast\n  forecast: string;\n}>;",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var input, output any
			if err := json.Unmarshal([]byte(tc.input), &input); err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal([]byte(tc.output), &output); err != nil {
				t.Fatal(err)
			}
			sample, err := renderCodeModeSample(tc.name, "Test tool", "function", input, output)
			if err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(sample, tc.expected) {
				t.Fatalf("sample %q lacks %q", sample, tc.expected)
			}
		})
	}
}

func TestRenderJSONSchemaTypeScript(t *testing.T) {
	for _, tc := range []struct{ schema, expected string }{
		{`{"type":"string","enum":["a","b"]}`, `"a" | "b"`},
		{`{"type":["string","null"]}`, `string | null`},
		{`{"type":"object","properties":{"a-b":{"type":"integer"}}}`, `{ "a-b"?: number; }`},
		{`{"type":"object","additionalProperties":{"type":"boolean"}}`, `{ [key: string]: boolean; }`},
		{`{"type":"object"}`, `{ [key: string]: unknown; }`},
		{`{"allOf":[{"anyOf":[{"type":"string"},{"type":"number"}]},{"type":"object","properties":{}}]}`, `(string | number) & { [key: string]: unknown; }`},
		{`{"$ref":"#/$defs/x"}`, `unknown`},
		{`false`, `never`},
	} {
		var schema any
		if err := json.Unmarshal([]byte(tc.schema), &schema); err != nil {
			t.Fatal(err)
		}
		if got := renderJSONSchemaTypeScript(schema); got != tc.expected {
			t.Errorf("render(%s) = %q, want %q", tc.schema, got, tc.expected)
		}
	}
}

func TestNormalizeCodeModeIdentifier(t *testing.T) {
	for input, expected := range map[string]string{"mcp__ologs__get_profile": "mcp__ologs__get_profile", "hidden-dynamic-tool": "hidden_dynamic_tool", "1st": "_st", "": "_"} {
		if got := normalizeCodeModeIdentifier(input); got != expected {
			t.Errorf("normalize(%q) = %q, want %q", input, got, expected)
		}
	}
}

func TestParseExecSource(t *testing.T) {
	ten, thousand := 10, 1000
	for _, tc := range []struct {
		name, input string
		expected    ExecSource
	}{
		{"plain", "text('hi')", ExecSource{Code: "text('hi')", YieldTimeMS: defaultExecYieldTimeMS}},
		{"pragma", "// @exec: {\"yield_time_ms\": 10}\ntext('hi')", ExecSource{Code: "text('hi')", YieldTimeMS: ten}},
		{"indented both fields", "  // @exec: {\"yield_time_ms\": 10, \"max_output_tokens\": 1000}\na\nb", ExecSource{Code: "a\nb", YieldTimeMS: ten, MaxOutputTokens: &thousand}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ParseExecSource(tc.input)
			if err != nil {
				t.Fatal(err)
			}
			if got.Code != tc.expected.Code || got.YieldTimeMS != tc.expected.YieldTimeMS || !equalIntPointer(got.MaxOutputTokens, tc.expected.MaxOutputTokens) {
				t.Fatalf("ParseExecSource(%q) = %+v, want %+v", tc.input, got, tc.expected)
			}
		})
	}
	for _, input := range []string{
		"  \n",
		"// @exec: {\"yield_time_ms\": 10}\n  ",
		"// @exec:\ncode",
		"// @exec: [1]\ncode",
		"// @exec: {\"unknown\": 1}\ncode",
		"// @exec: {\"yield_time_ms\": -1}\ncode",
		"// @exec: {\"max_output_tokens\": 1.5}\ncode",
		"// @exec: {\"yield_time_ms\": 9007199254740992}\ncode",
	} {
		if _, err := ParseExecSource(input); err == nil {
			t.Errorf("ParseExecSource(%q) accepted invalid input", input)
		}
	}
}

func TestParseWaitArgumentsAppliesCodexDefaults(t *testing.T) {
	parsed, err := ParseWaitArguments(`{"cell_id":"cell"}`)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.YieldTimeMS != defaultWaitYieldTimeMS || parsed.MaxTokens != nil || parsed.Terminate {
		t.Fatalf("wait defaults = %+v", parsed)
	}
	parsed, err = ParseWaitArguments(`{"cell_id":"cell","yield_time_ms":5,"max_tokens":7,"terminate":true}`)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.YieldTimeMS != 5 || parsed.MaxTokens == nil || *parsed.MaxTokens != 7 || !parsed.Terminate {
		t.Fatalf("wait arguments = %+v", parsed)
	}
	for _, input := range []string{`{}`, `{"cell_id":"cell","yield_time_ms":-1}`, `{"cell_id":"cell","max_tokens":0.5}`} {
		if _, err := ParseWaitArguments(input); err == nil {
			t.Errorf("ParseWaitArguments(%s) accepted invalid input", input)
		}
	}
}

func equalIntPointer(left, right *int) bool {
	return left == nil && right == nil || left != nil && right != nil && *left == *right
}
