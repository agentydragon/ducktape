package mcpexec

import (
	"bytes"
	"encoding/json"
	"testing"

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

// One tool the catalog cannot hold is left out, with its reason; the others are still offered.
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
	if _, offered := tools["test_good"]; len(tools) != 1 || !offered {
		t.Errorf("offers %v, want only test_good", tools)
	}
	if len(skip) != 2 || skip["test_bad"] == "" || skip["test_twice"] != "duplicate MCP tool name" {
		t.Errorf("skipped %v, want test_bad and test_twice", skip)
	}
}

// With nothing left to offer, the list is invalid, as before.
func TestAListOfOnlyToolsTheCatalogCannotHoldIsInvalid(t *testing.T) {
	bad := json.RawMessage(`{"type": "object", "properties": {"test": {"type": 1}}}`)
	if _, _, err := mirror([]*mcp.Tool{{Name: "test_bad", InputSchema: bad}}); err == nil {
		t.Error("a list of only bad tools was accepted")
	}
}
