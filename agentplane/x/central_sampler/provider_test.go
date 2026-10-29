package centralsampler

import (
	"context"
	"encoding/json"
	"slices"
	"strings"
	"testing"
)

func TestProviderKeepsOpaqueLargeJSONNumbersExact(t *testing.T) {
	stream := strings.Join([]string{
		`data: {"type":"response.output_item.done","item":{"type":"reasoning","opaque_sequence":9007199254740993}}`,
		"",
		`data: {"type":"response.completed","response":{"status":"completed","output":[{"type":"reasoning","opaque_sequence":9007199254740993}]}}`,
		"",
		"",
	}, "\n")
	response, err := readProviderStream(context.Background(), strings.NewReader(stream), nil)
	if err != nil {
		t.Fatal(err)
	}
	if got := canonicalJSON(response.Output); !strings.Contains(got, `"opaque_sequence":9007199254740993`) {
		t.Fatalf("opaque provider number changed during decoding: %s", got)
	}
	if got := canonicalJSON(response.Audit()); !strings.Contains(got, `"opaque_sequence":9007199254740993`) {
		t.Fatalf("opaque provider number changed in audit evidence: %s", got)
	}
}

func TestResponsesRequestUsesCodexToolsAndPreservesOpaqueContext(t *testing.T) {
	var item ResponsesItem
	if err := json.Unmarshal([]byte(`{"type":"reasoning","encrypted_content":"opaque","provider_sequence":9007199254740993}`), &item); err != nil {
		t.Fatal(err)
	}
	body, err := RequestBody("fixture-model", "fixture-session", []ResponsesItem{item}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if body.Instructions == "" {
		t.Fatal("request has no Codex base instructions")
	}
	encoded, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	tools := map[string]ResponsesAdditionalTool{}
	for _, tool := range body.Input[0].Tools {
		tools[tool.Name] = tool
	}
	if exec := tools["exec"]; exec.Type != "custom" || exec.Format == nil || exec.Format.Type != "grammar" || exec.Format.Syntax != "lark" {
		t.Fatalf("exec is not a Lark-grammar freeform tool: %+v", exec)
	}
	wait := tools["wait"]
	var parameters struct {
		Required []string `json:"required"`
	}
	if err := json.Unmarshal(wait.Parameters, &parameters); err != nil {
		t.Fatal(err)
	}
	if wait.Type != "function" || !slices.Equal(parameters.Required, []string{"cell_id"}) {
		t.Fatalf("wait is not a function tool requiring cell_id: %+v", wait)
	}
	for _, expected := range []string{
		`"type":"additional_tools"`,
		`"include":["reasoning.encrypted_content"]`,
		`"store":false`,
		`"stream":true`,
		`"encrypted_content":"opaque"`,
		`"provider_sequence":9007199254740993`,
	} {
		if !strings.Contains(string(encoded), expected) {
			t.Fatalf("Responses request lost %s: %s", expected, encoded)
		}
	}
}

func TestResponsesItemRetainsImageAndAudioContent(t *testing.T) {
	input := []byte(`[{"type":"input_image","image_url":"data:image/png;base64,opaque","detail":"high"},{"type":"input_audio","data":"opaque-audio","format":"wav"}]`)
	var content []ResponsesContentPart
	if err := json.Unmarshal(input, &content); err != nil {
		t.Fatal(err)
	}
	item := ResponsesItem{Type: ResponsesItemCustomToolOutput, CallID: "call-1", Output: content}
	encoded, err := json.Marshal(item)
	if err != nil {
		t.Fatal(err)
	}
	for _, expected := range []string{`"type":"input_image"`, `"image_url":"data:image/png;base64,opaque"`, `"type":"input_audio"`, `"format":"wav"`} {
		if !strings.Contains(string(encoded), expected) {
			t.Fatalf("Responses item lost %s: %s", expected, encoded)
		}
	}
}
