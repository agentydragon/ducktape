package centralsampler

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
)

func TestExecDescriptionDeclaresEveryNestedToolInput(t *testing.T) {
	definitions := toolDefinitions()
	description, err := execToolDescription(definitions, protocol.ExecEncodingFreeform)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(description, "{{") {
		t.Fatalf("exec description kept a template placeholder: %s", description)
	}
	for _, definition := range definitions {
		if !strings.Contains(description, "### `"+definition.Name+"`") {
			t.Fatalf("exec description has no section for %s", definition.Name)
		}
		declaration := definition.Name + "(input: string)"
		if definition.Kind == "function" {
			schema, err := schemaValue(definition.InputSchema)
			if err != nil {
				t.Fatal(err)
			}
			for property := range schema.(map[string]any)["properties"].(map[string]any) {
				if !strings.Contains(description, property) {
					t.Fatalf("exec description does not declare %s.%s", definition.Name, property)
				}
			}
			declaration = definition.Name + "(args: "
		}
		if !strings.Contains(description, "declare const tools: { "+declaration) {
			t.Fatalf("exec description lacks %s declaration: %s", definition.Name, description)
		}
	}
}

func TestHostToolDefinitionsCarryDeclarationsWithoutSchemas(t *testing.T) {
	definitions, err := hostToolDefinitions(toolDefinitions())
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
	for _, definition := range wire {
		if definition["input_schema"] != nil || definition["output_schema"] != nil {
			t.Fatalf("host definition kept a schema: %v", definition)
		}
		if !strings.Contains(definition["description"].(string), "exec tool declaration:") {
			t.Fatalf("host definition description lacks its declaration: %v", definition)
		}
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
		expected    execSource
	}{
		{"plain", "text('hi')", execSource{code: "text('hi')"}},
		{"pragma", "// @exec: {\"yield_time_ms\": 10}\ntext('hi')", execSource{code: "text('hi')", yieldTimeMS: &ten}},
		{"indented both fields", "  // @exec: {\"yield_time_ms\": 10, \"max_output_tokens\": 1000}\na\nb", execSource{code: "a\nb", yieldTimeMS: &ten, maxOutputTokens: &thousand}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parseExecSource(tc.input)
			if err != nil {
				t.Fatal(err)
			}
			if got.code != tc.expected.code || !equalIntPointer(got.yieldTimeMS, tc.expected.yieldTimeMS) || !equalIntPointer(got.maxOutputTokens, tc.expected.maxOutputTokens) {
				t.Fatalf("parseExecSource(%q) = %+v, want %+v", tc.input, got, tc.expected)
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
		if _, err := parseExecSource(input); err == nil {
			t.Errorf("parseExecSource(%q) accepted invalid input", input)
		}
	}
}

func TestParseWaitArgumentsAppliesCodexDefaults(t *testing.T) {
	parsed, err := parseWaitArguments(`{"cell_id":"cell"}`)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.yieldTimeMS != defaultWaitYieldTimeMS || parsed.maxTokens != nil || parsed.terminate {
		t.Fatalf("wait defaults = %+v", parsed)
	}
	parsed, err = parseWaitArguments(`{"cell_id":"cell","yield_time_ms":5,"max_tokens":7,"terminate":true}`)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.yieldTimeMS != 5 || parsed.maxTokens == nil || *parsed.maxTokens != 7 || !parsed.terminate {
		t.Fatalf("wait arguments = %+v", parsed)
	}
	for _, input := range []string{`{}`, `{"cell_id":"cell","yield_time_ms":-1}`, `{"cell_id":"cell","max_tokens":0.5}`} {
		if _, err := parseWaitArguments(input); err == nil {
			t.Errorf("parseWaitArguments(%s) accepted invalid input", input)
		}
	}
}

func equalIntPointer(left, right *int) bool {
	return left == nil && right == nil || left != nil && right != nil && *left == *right
}
