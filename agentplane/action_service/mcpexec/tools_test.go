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
	tools, err := mirror([]*mcp.Tool{{Name: "test_tool", InputSchema: json.RawMessage(`{
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
