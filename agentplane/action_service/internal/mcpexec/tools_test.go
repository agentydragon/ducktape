package mcpexec

import (
	"bytes"
	"encoding/json"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/santhosh-tekuri/jsonschema/v6"
)

// Execution checks the arguments again against the live schema; a check stricter than admission's would fail a
// request admission let through.
func TestExecutionChecksArgumentsAsAdmissionDoes(t *testing.T) {
	tools, _, err := mirror([]*mcp.Tool{{Name: "test_tool", InputSchema: json.RawMessage(`{
		"$schema": "http://json-schema.org/draft-07/schema#",
		"type": "object",
		"properties": {"address": {"type": "string", "format": "email"}}
	}`)}})
	if err != nil {
		t.Fatal(err)
	}
	for arguments, valid := range map[string]bool{
		// Neither asserts a format, as Python's jsonschema.validate asserts none.
		`{"address": "test-not-an-address"}`: true,
		`{"address": 1}`:                     false,
	} {
		decoded, err := jsonschema.UnmarshalJSON(bytes.NewReader([]byte(arguments)))
		if err != nil {
			t.Fatal(err)
		}
		admitted := tools["test_tool"].definition.CheckArguments(decoded) == nil
		if executed := tools["test_tool"].matches(json.RawMessage(arguments)); admitted != valid || executed != valid {
			t.Errorf("%s: admitted %v, executed %v; want %v", arguments, admitted, executed, valid)
		}
	}
}

// An invalid schema remains available with a warning; an ambiguous duplicate is still omitted.
func TestAToolTheCatalogCannotHoldDoesNotTakeTheOthersWithIt(t *testing.T) {
	good := json.RawMessage(`{"type": "object"}`)
	bad := json.RawMessage(`{"type": "object", "properties": {"test": {"type": 1}}}`)
	tools, skip, err := mirror([]*mcp.Tool{
		{Name: "test_good", InputSchema: good},
		{Name: "test_bad", InputSchema: bad},
		{Name: "test_twice", InputSchema: good},
		{Name: "test_twice", InputSchema: good},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, offered := tools["test_good"]; len(tools) != 2 || !offered {
		t.Errorf("offers %v, want test_good and test_bad", tools)
	}
	if len(skip) != 1 || skip["test_twice"] != "duplicate MCP tool name" {
		t.Errorf("skipped %v, want test_twice", skip)
	}
	badTool := tools["test_bad"]
	if badTool.definition.InputSchemaValidation.GetStatus() != action_servicepb.InputSchemaValidationStatus_INPUT_SCHEMA_VALIDATION_STATUS_NOT_ENFORCED || badTool.arguments != nil {
		t.Fatal("bad schema did not receive an unvalidated warning")
	}
	if !badTool.matches(json.RawMessage(`{"test": 42}`)) || badTool.matches(json.RawMessage(`null`)) {
		t.Error("fallback must accept JSON object arguments only")
	}
	if err := badTool.definition.CheckArguments(map[string]any{"test": 42}); err != nil {
		t.Errorf("admission rejected arguments to unvalidated tool: %v", err)
	}
}

func TestAListOfOnlyToolsWithUnusableSchemasIsOffered(t *testing.T) {
	bad := json.RawMessage(`{"type": "object", "properties": {"test": {"type": 1}}}`)
	if tools, _, err := mirror([]*mcp.Tool{{Name: "test_bad", InputSchema: bad}}); err != nil || tools["test_bad"].definition.InputSchemaValidation.GetStatus() != action_servicepb.InputSchemaValidationStatus_INPUT_SCHEMA_VALIDATION_STATUS_NOT_ENFORCED {
		t.Errorf("schema fallback = %v, %v", tools, err)
	}
}

func TestUnsupportedLookaheadRemainsDiscoverable(t *testing.T) {
	schema := json.RawMessage(`{"type":"object","properties":{"email":{"type":"string","pattern":"^(?!\\\\.)"}}}`)
	tools, _, err := mirror([]*mcp.Tool{{Name: "search_nodes", InputSchema: schema}})
	if err != nil || tools["search_nodes"].definition.InputSchemaValidation.GetStatus() != action_servicepb.InputSchemaValidationStatus_INPUT_SCHEMA_VALIDATION_STATUS_NOT_ENFORCED {
		t.Fatalf("lookahead fallback = %v, %v", tools, err)
	}
	tools, _, err = mirror([]*mcp.Tool{{Name: "search_nodes", InputSchema: json.RawMessage(`{"type":"object"}`)}})
	if err != nil || tools["search_nodes"].definition.InputSchemaValidation.GetStatus() != action_servicepb.InputSchemaValidationStatus_INPUT_SCHEMA_VALIDATION_STATUS_ENFORCED || tools["search_nodes"].arguments == nil {
		t.Fatalf("corrected schema still has a warning = %v, %v", tools, err)
	}
}

// A Tana-style search schema with a lookahead must be discoverable and checked the same way at admission and execution.
func TestSearchNodesPatternIsOfferedAndChecked(t *testing.T) {
	const schema = `{"$schema":"http://json-schema.org/draft-07/schema#","type":"object","properties":{"query":{"type":"object","properties":{"edited":{"type":"object","properties":{"by":{"type":"string","pattern":"^(?!\\.)(?!.*\\.\\.)([A-Za-z0-9_'+\\-\\.]*)[A-Za-z0-9_+-]@([A-Za-z0-9][A-Za-z0-9\\-]*\\.)+[A-Za-z]{2,}$"}}}}}}}`
	tools, skipped, err := mirror([]*mcp.Tool{{Name: "search_nodes", InputSchema: json.RawMessage(schema)}})
	if err != nil {
		t.Fatal(err)
	}
	if len(skipped) != 0 || len(tools) != 1 {
		t.Fatalf("tools %v skipped %v", tools, skipped)
	}
	tool := tools["search_nodes"]
	for raw, want := range map[string]bool{
		`{"query":{"edited":{"by":"first.last@example.org"}}}`:  true,
		`{"query":{"edited":{"by":"first..last@example.org"}}}`: false,
	} {
		value, err := jsonschema.UnmarshalJSON(bytes.NewReader([]byte(raw)))
		if err != nil {
			t.Fatal(err)
		}
		admitted := tool.definition.CheckArguments(value) == nil
		executed := tool.matches(json.RawMessage(raw))
		if admitted != want || executed != want {
			t.Errorf("%s admitted=%v executed=%v want=%v", raw, admitted, executed, want)
		}
	}
}
