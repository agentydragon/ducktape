package responses

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
)

func testToolSurface(encoding protocol.ExecEncoding) protocol.ToolSurface {
	description := "Execute JavaScript source."
	if encoding == protocol.ExecEncodingFreeform {
		description += " First-line pragma: // @exec: {\"yield_time_ms\": 10000}."
	}
	return protocol.ToolSurface{
		Description:     description,
		Parameters:      json.RawMessage(`{"type":"object","properties":{"code":{"type":"string"}},"required":["code"]}`),
		FreeformGrammar: "start: SOURCE\nSOURCE: /[\\s\\S]+/",
		WaitDescription: "Waits for a yielded cell.",
		WaitParameters:  json.RawMessage(`{"type":"object","properties":{"cell_id":{"type":"string"}},"required":["cell_id"]}`),
	}
}

func TestCompletedItemTextJoinsReasoningSummaryParts(t *testing.T) {
	for _, test := range []struct {
		item string
		want string
	}{
		{`{"type":"reasoning","summary":[{"type":"summary_text","text":"**Plan**"},{"type":"summary_text","text":"Read the file."}]}`, "**Plan**\nRead the file."},
		{`{"type":"message","role":"assistant","content":[{"type":"output_text","text":"one "},{"type":"output_text","text":"answer"}]}`, "one answer"},
	} {
		var item Item
		if err := json.Unmarshal([]byte(test.item), &item); err != nil {
			t.Fatal(err)
		}
		got, err := Text(item)
		if err != nil {
			t.Fatal(err)
		}
		if got != test.want {
			t.Fatalf("Text(%s) = %q, want %q", test.item, got, test.want)
		}
	}
}

func TestProviderKeepsOpaqueLargeJSONNumbersExact(t *testing.T) {
	stream := strings.Join([]string{
		`data: {"type":"response.output_item.done","item":{"type":"reasoning","opaque_sequence":9007199254740993}}`,
		"",
		`data: {"type":"response.completed","response":{"status":"completed","output":[{"type":"reasoning","opaque_sequence":9007199254740993}]}}`,
		"",
		"",
	}, "\n")
	response, err := readStream(context.Background(), strings.NewReader(stream), nil)
	if err != nil {
		t.Fatal(err)
	}
	if got := protocol.CanonicalJSON(response.Output); !strings.Contains(got, `"opaque_sequence":9007199254740993`) {
		t.Fatalf("opaque provider number changed during decoding: %s", got)
	}
	if got := protocol.CanonicalJSON(response.Audit()); !strings.Contains(got, `"opaque_sequence":9007199254740993`) {
		t.Fatalf("opaque provider number changed in audit evidence: %s", got)
	}
}

func TestProviderAcceptsAntigravityLiteLLMTerminalOutputDifferences(t *testing.T) {
	// Observed through Antigravity behind LiteLLM's Responses API: item.done carries
	// a reasoning summary, while completed rewrites it and adds an unstreamed message.
	stream := strings.Join([]string{
		`data: {"type":"response.output_item.done","output_index":0,"item":{"id":"rs_fixture","type":"reasoning","summary":[{"type":"summary_text","text":"Inspect the workspace."}]}}`,
		"",
		`data: {"type":"response.output_item.done","output_index":1,"item":{"type":"function_call","id":"fc_fixture","call_id":"call_fixture","name":"exec","status":"completed","arguments":"{\"code\":\"text(1)\"}"}}`,
		"",
		`data: {"type":"response.completed","response":{"id":"resp_fixture","status":"completed","output":[{"type":"reasoning","id":"rs_fixture","status":"completed","role":"assistant","content":[{"type":"output_text","text":"Inspect the workspace."}],"encrypted_content":"fixture-opaque-reasoning"},{"type":"message","id":"msg_fixture","status":"completed","role":"assistant","content":[{"type":"output_text","text":"Terminal-only message."}]},{"type":"function_call","id":"fc_fixture","call_id":"call_fixture","name":"exec","status":"completed","arguments":"{\"code\":\"text(1)\"}"}]}}`,
		"",
		"",
	}, "\n")
	response, err := readStream(context.Background(), strings.NewReader(stream), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(response.Output) != 2 {
		t.Fatalf("expected only streamed completed items, got %s", protocol.CanonicalJSON(response.Output))
	}
	reasoning, call := response.Output[0], response.Output[1]
	text, err := Text(reasoning)
	if err != nil {
		t.Fatal(err)
	}
	if reasoning.Type != ItemReasoning || text != "Inspect the workspace." || reasoning.EncryptedContent != "" || len(reasoning.Content) != 0 {
		t.Fatalf("streamed reasoning was replaced: %s", protocol.CanonicalJSON(reasoning))
	}
	if call.Type != ItemFunctionCall || call.CallID != "call_fixture" || call.Name != "exec" || call.Arguments != `{"code":"text(1)"}` {
		t.Fatalf("streamed tool call changed: %s", protocol.CanonicalJSON(call))
	}
	var terminal struct {
		Output []Item `json:"output"`
	}
	if err := json.Unmarshal([]byte(protocol.CanonicalJSON(response.Terminal)), &terminal); err != nil {
		t.Fatal(err)
	}
	if len(terminal.Output) != 3 || terminal.Output[0].EncryptedContent != "fixture-opaque-reasoning" {
		t.Fatalf("terminal evidence was not retained: %s", protocol.CanonicalJSON(response.Terminal))
	}
}

func TestResponsesRequestUsesCodexToolsAndPreservesOpaqueContext(t *testing.T) {
	var item Item
	if err := json.Unmarshal([]byte(`{"type":"reasoning","encrypted_content":"opaque","provider_sequence":9007199254740993}`), &item); err != nil {
		t.Fatal(err)
	}
	body, err := BuildRequest(RequestParams{Model: "fixture-model", Attribution: protocol.SessionAttribution{SessionID: "fixture-session"}, Items: []Item{item}, Tools: testToolSurface(protocol.ExecEncodingFreeform)}, Config{Dialect: DialectLite}, protocol.ExecEncodingFreeform)
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
	tools := map[string]AdditionalTool{}
	for _, tool := range body.Input[0].(Item).Tools {
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
		// The provider rejects Responses Lite requests without these.
		`"context":"all_turns"`,
		`"parallel_tool_calls":false`,
		`"encrypted_content":"opaque"`,
		`"provider_sequence":9007199254740993`,
	} {
		if !strings.Contains(string(encoded), expected) {
			t.Fatalf("Responses request lost %s: %s", expected, encoded)
		}
	}
}

func TestResponsesRequestBuildsReasoningAndAttribution(t *testing.T) {
	for _, dialect := range []Dialect{DialectLite, DialectStandard} {
		for _, effort := range []string{"", "high"} {
			t.Run(string(dialect)+"/"+effort, func(t *testing.T) {
				attribution := protocol.SessionAttribution{SessionID: "session", ThreadID: "thread", TurnID: "turn"}
				params := RequestParams{Model: "fixture", Effort: effort, Attribution: attribution, Tools: testToolSurface(protocol.ExecEncodingJSON)}
				config := Config{Dialect: dialect}
				body, err := BuildRequest(params, config, protocol.ExecEncodingJSON)
				if err != nil {
					t.Fatal(err)
				}
				if body.PromptCacheKey != attribution.SessionID {
					t.Fatalf("prompt cache key = %q", body.PromptCacheKey)
				}
				if dialect == DialectLite {
					want := ClientMetadata{SessionID: "session", ThreadID: "thread", TurnID: "turn"}
					if body.ClientMetadata == nil || *body.ClientMetadata != want {
						t.Fatalf("client metadata = %+v", body.ClientMetadata)
					}
				} else if body.ClientMetadata != nil {
					t.Fatal("standard request includes private metadata")
				}
				if dialect == DialectLite || effort != "" {
					if body.Reasoning == nil || body.Reasoning.Effort != effort || !slices.Equal(body.Include, []string{"reasoning.encrypted_content"}) {
						t.Fatalf("reasoning = %+v, include = %v", body.Reasoning, body.Include)
					}
					wantContext, wantSummary := "", ""
					if dialect == DialectLite {
						wantContext, wantSummary = "all_turns", codexReasoningSummary
					}
					if body.Reasoning.Context != wantContext || body.Reasoning.Summary != wantSummary {
						t.Fatalf("reasoning dialect settings = %+v", body.Reasoning)
					}
				} else if body.Reasoning != nil || len(body.Include) != 0 {
					t.Fatal("standard request without effort includes reasoning configuration")
				}
				compaction, err := BuildCompactionRequest(params, config, protocol.ExecEncodingJSON)
				if dialect == DialectStandard {
					if err == nil {
						t.Fatal("standard compaction accepted")
					}
				} else {
					if err != nil {
						t.Fatal(err)
					}
					if compaction.Input[len(compaction.Input)-1].(Item).Type != ItemCompactionTrigger || *compaction.ClientMetadata != *body.ClientMetadata {
						t.Fatalf("compaction request = %+v", compaction)
					}
				}
			})
		}
	}
}

func TestValidateCompactionPrependsCurrentEnvironment(t *testing.T) {
	original := &Context{}
	if err := original.AppendRecords([]json.RawMessage{
		[]byte(`{"type":"message","role":"user","content":"a sufficiently long original context"}`),
		[]byte(`{"type":"message","role":"assistant","content":"old response"}`),
	}); err != nil {
		t.Fatal(err)
	}
	records := []json.RawMessage{[]byte(`{"type":"compaction","encrypted_content":"opaque","provider_number":9007199254740993}`)}
	replacement, err := ValidateCompaction(original, records, &Audit{Events: []Event{}}, "Current environment")
	if err != nil {
		t.Fatal(err)
	}
	if len(replacement.Items) != 2 || replacement.Items[0].Type != ItemMessage || replacement.Items[0].Role != "user" || replacement.Items[1].Type != ItemCompaction {
		t.Fatalf("compacted context has unexpected boundary items: %+v", replacement.Items)
	}
	if got := string(replacement.Items[1].Raw); got != string(records[0]) {
		t.Fatalf("opaque compaction record changed: %s", got)
	}
}

func TestValidateCompactionRejectsUnusableSummary(t *testing.T) {
	original := &Context{Items: []Item{{Type: ItemMessage, Role: "user", Content: json.RawMessage(`"long original context"`)}}}
	for _, records := range [][]json.RawMessage{
		{},
		{[]byte(`{"type":"message","role":"assistant","content":"not a compaction"}`)},
		{[]byte(`{"type":"compaction"}`)},
	} {
		if _, err := ValidateCompaction(original, records, nil, "environment"); err == nil {
			t.Fatalf("accepted unusable summary %s", records)
		}
	}
}

func TestDialectAndExecEncodingAreIndependent(t *testing.T) {
	for _, mode := range []Dialect{DialectStandard, DialectLite} {
		for _, encoding := range []protocol.ExecEncoding{protocol.ExecEncodingJSON, protocol.ExecEncodingFreeform} {
			t.Run(string(mode)+"/"+string(encoding), func(t *testing.T) {
				config := Config{Dialect: mode}
				body, err := BuildRequest(RequestParams{Model: "fixture", Attribution: protocol.SessionAttribution{SessionID: "session"}, Tools: testToolSurface(encoding)}, config, encoding)
				if err != nil {
					t.Fatal(err)
				}
				encoded, err := json.Marshal(body)
				if err != nil {
					t.Fatal(err)
				}
				var wire map[string]json.RawMessage
				if err := json.Unmarshal(encoded, &wire); err != nil {
					t.Fatal(err)
				}
				tools := body.Tools
				if mode == DialectLite {
					tools = body.Input[0].(Item).Tools
					if _, present := wire["tools"]; present {
						t.Fatal("Lite declares top-level tools")
					}
				} else {
					if string(wire["input"]) != "[]" {
						t.Fatalf("empty standard input: %s", wire["input"])
					}
					for _, private := range []string{"client_metadata", "reasoning", "include"} {
						if _, present := wire[private]; present {
							t.Fatalf("standard request includes %s", private)
						}
					}
					if strings.Contains(string(wire["input"]), "additional_tools") {
						t.Fatal("standard request includes additional_tools")
					}
				}
				execDescription := tools[0].Description
				if strings.Contains(execDescription, "{{") {
					t.Fatal("unresolved exec description template")
				}
				if strings.Contains(execDescription, "// @exec:") != (encoding == protocol.ExecEncodingFreeform) {
					t.Fatal("exec description has wrong input contract")
				}
				if encoding == protocol.ExecEncodingJSON {
					if len(tools) != 1 || tools[0].Name != "exec" || tools[0].Type != "function" {
						t.Fatalf("JSON tools: %+v", tools)
					}
					if strings.Contains(tools[0].Description, "Accepts raw JavaScript") {
						t.Fatal("JSON exec advertises raw input")
					}
				} else if len(tools) != 2 || tools[0].Type != "custom" || tools[1].Name != "wait" {
					t.Fatalf("freeform tools: %+v", tools)
				}
				for _, call := range []protocol.ToolInvocation{
					{Encoding: protocol.ExecEncodingJSON, Name: "exec"},
					{Encoding: protocol.ExecEncodingFreeform, Name: "exec"},
					{Encoding: protocol.ExecEncodingJSON, Name: "wait"},
				} {
					want := encoding == protocol.ExecEncodingJSON && call.Name == "exec" && call.Encoding == protocol.ExecEncodingJSON ||
						encoding == protocol.ExecEncodingFreeform && (call.Name == "exec" && call.Encoding == protocol.ExecEncodingFreeform || call.Name == "wait" && call.Encoding == protocol.ExecEncodingJSON)
					if call.IsModelTool(encoding) != want {
						t.Fatalf("wrong tool validation: %+v", call)
					}
					wantConfiguredName := call.Name == "exec" || encoding == protocol.ExecEncodingFreeform && call.Name == "wait"
					if call.IsConfiguredToolName(encoding) != wantConfiguredName {
						t.Fatalf("wrong configured-name validation: %+v", call)
					}
				}
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.URL.Path != "/v1/responses" {
						t.Errorf("Responses request path: %q", r.URL.Path)
					}
					if got := r.Header.Get("x-openai-internal-codex-responses-lite"); (got == "true") != (mode == DialectLite) {
						t.Errorf("Lite header: %q", got)
					}
					_, _ = w.Write([]byte("data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\",\"output\":[]}}\n\n"))
				}))
				defer server.Close()
				if _, err := Sample(context.Background(), server.Client(), server.URL, "fixture", body, config, nil, nil, false); err != nil {
					t.Fatal(err)
				}
			})
		}
	}
}

func TestStandardResponsesSerializesTextAndAsyncNotifications(t *testing.T) {
	state := &Context{}
	if err := state.AppendRecords([]json.RawMessage{[]byte(`{"type":"function_call","name":"exec","call_id":"call","arguments":"{\"code\":\"text(1)\"}"}`)}); err != nil {
		t.Fatal(err)
	}
	call, err := state.FindCall("call")
	if err != nil {
		t.Fatal(err)
	}
	if err := state.AppendToolResult(call, []protocol.ContentPart{{Type: "input_text", Text: "result"}}); err != nil {
		t.Fatal(err)
	}
	if err := state.AppendToolResult(call, []protocol.ContentPart{{Type: "input_text", Text: "later"}}); err != nil {
		t.Fatal(err)
	}
	items := state.Items
	body, err := BuildRequest(RequestParams{Model: "fixture", Attribution: protocol.SessionAttribution{SessionID: "session"}, Items: items, Tools: testToolSurface(protocol.ExecEncodingJSON)}, Config{Dialect: DialectStandard}, protocol.ExecEncodingJSON)
	if err != nil {
		t.Fatal(err)
	}
	encodedBytes, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	encoded := string(encodedBytes)
	for _, expected := range []string{`"output":"result"`, `"role":"developer"`, `Asynchronous output from exec call call:\nlater`} {
		if !strings.Contains(encoded, expected) {
			t.Fatalf("missing %s in %s", expected, encoded)
		}
	}
	if strings.Contains(encoded, "custom_tool_call_output") {
		t.Fatal("standard notification became a duplicate tool output")
	}
	if items[1].Type != ItemFunctionOutput || items[2].Type != ItemFunctionOutput {
		t.Fatal("async outputs did not retain the function-call encoding")
	}
}

func TestToolInvocationShapeSeparatesUnknownNamesFromMalformedItems(t *testing.T) {
	for _, test := range []struct {
		name         string
		item         string
		wantValid    bool
		wantEncoding protocol.ExecEncoding
	}{
		{
			name:         "unknown custom tool has an answerable shape",
			item:         `{"type":"custom_tool_call","call_id":"custom-1","name":"missing_custom","input":"{}"}`,
			wantValid:    true,
			wantEncoding: protocol.ExecEncodingFreeform,
		},
		{
			name:         "unknown function has an answerable shape",
			item:         `{"type":"function_call","call_id":"function-1","name":"missing_function","arguments":"{}"}`,
			wantValid:    true,
			wantEncoding: protocol.ExecEncodingJSON,
		},
		{
			name: "custom call without input is malformed",
			item: `{"type":"custom_tool_call","call_id":"custom-1","name":"missing_custom"}`,
		},
		{
			name: "custom call with null input is malformed",
			item: `{"type":"custom_tool_call","call_id":"custom-1","name":"missing_custom","input":null}`,
		},
		{
			name: "function call without arguments is malformed",
			item: `{"type":"function_call","call_id":"function-1","name":"missing_function"}`,
		},
		{
			name: "tool call without call ID is malformed",
			item: `{"type":"function_call","name":"missing_function","arguments":"{}"}`,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			var item Item
			if err := json.Unmarshal([]byte(test.item), &item); err != nil {
				t.Fatal(err)
			}
			if got := item.HasValidToolCallShape(); got != test.wantValid {
				t.Fatalf("HasValidToolCallShape()=%t, want %t", got, test.wantValid)
			}
			if test.wantValid {
				call, ok := item.ToolCall()
				if !ok || call.Encoding != test.wantEncoding {
					t.Fatalf("tool call=%+v, want encoding %s", call, test.wantEncoding)
				}
			}
		})
	}
}

func TestContextRetainsNativeRawRecordsAndNumericPrecision(t *testing.T) {
	state := &Context{}
	input := json.RawMessage(` {"type":"reasoning","provider_data":{"large":9007199254740993}} `)
	if err := state.AppendRecords([]json.RawMessage{input}); err != nil {
		t.Fatal(err)
	}
	records, err := state.Records()
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 || !strings.Contains(string(records[0]), `9007199254740993`) || !strings.Contains(string(records[0]), `"provider_data"`) {
		t.Fatalf("raw provider fields or number precision lost: %s", records)
	}
	if !bytes.Contains(records[0], []byte(`"type":"reasoning"`)) {
		t.Fatalf("native Responses item not retained: %s", records[0])
	}
}

func TestContextRejectsMarkersAndUnknownRecordsAtomically(t *testing.T) {
	for _, typeName := range []string{
		"anthropic_message", "reasoning_effort", "reasoning_effort_restore", "execution_state",
		"additional_tools", "compaction_trigger", "unknown_record",
	} {
		t.Run(typeName, func(t *testing.T) {
			state := &Context{}
			if err := state.AppendRecords([]json.RawMessage{[]byte(`{"type":"message","role":"user","content":[]}`)}); err != nil {
				t.Fatal(err)
			}
			err := state.AppendRecords([]json.RawMessage{
				[]byte(`{"type":"message","role":"assistant","content":[]}`),
				[]byte(`{"type":"` + typeName + `"}`),
			})
			if err == nil {
				t.Fatalf("accepted non-native type %q", typeName)
			}
			if len(state.Items) != 1 {
				t.Fatalf("failed append partially mutated context: %+v", state.Items)
			}
		})
	}
}

func TestContextTracksPendingCallsAndRepeatedOutputs(t *testing.T) {
	state := &Context{}
	if err := state.AppendRecords([]json.RawMessage{
		[]byte(`{"type":"message","role":"user","content":[]}`),
		[]byte(`{"type":"function_call","call_id":"call-1","name":"exec","arguments":"{}"}`),
	}); err != nil {
		t.Fatal(err)
	}
	pending, err := state.PendingCalls()
	if err != nil || len(pending) != 1 || pending[0].SourceRecord != 2 || pending[0].Encoding != protocol.ExecEncodingJSON {
		t.Fatalf("pending calls=%+v, error=%v", pending, err)
	}
	call, err := state.FindCall("call-1")
	if err != nil || call.SourceRecord != 2 {
		t.Fatalf("found call=%+v, error=%v", call, err)
	}
	if _, err := state.FindCall("missing"); !errors.Is(err, protocol.ErrMissing) {
		t.Fatalf("unknown call error=%v, want protocol.ErrMissing", err)
	}
	parts := []protocol.ContentPart{{Type: "input_text", Text: "first"}}
	if err := state.AppendToolResult(call, parts); err != nil {
		t.Fatal(err)
	}
	if err := state.AppendToolResult(call, []protocol.ContentPart{{Type: "input_text", Text: "async"}}); err != nil {
		t.Fatal(err)
	}
	if state.Items[2].Type != ItemFunctionOutput || state.Items[3].Type != ItemFunctionOutput {
		t.Fatalf("tool outputs=%+v", state.Items[2:])
	}
	if err := state.CheckSettled(); err != nil {
		t.Fatalf("repeated async output left call unsettled: %v", err)
	}
}

func TestContextUsesCustomOutputForFreeformCalls(t *testing.T) {
	state := &Context{}
	if err := state.AppendRecords([]json.RawMessage{
		[]byte(`{"type":"custom_tool_call","call_id":"exec-1","name":"exec","input":"text(1)"}`),
	}); err != nil {
		t.Fatal(err)
	}
	call, err := state.FindCall("exec-1")
	if err != nil {
		t.Fatal(err)
	}
	if err := state.AppendToolResult(call, []protocol.ContentPart{{Type: "input_text", Text: "1"}}); err != nil {
		t.Fatal(err)
	}
	if got := state.Items[1].Type; got != ItemCustomToolOutput {
		t.Fatalf("freeform result type=%s, want %s", got, ItemCustomToolOutput)
	}
}

func TestContextRejectsDuplicateOrOrphanToolRecords(t *testing.T) {
	call := []byte(`{"type":"function_call","call_id":"call-1","name":"exec","arguments":"{}"}`)
	functionOutput := []byte(`{"type":"function_call_output","call_id":"call-1","output":[]}`)
	for _, test := range []struct {
		name    string
		records [][]byte
	}{
		{name: "duplicate call", records: [][]byte{call, call}},
		{name: "orphan output", records: [][]byte{functionOutput}},
		{name: "wrong output encoding", records: [][]byte{call, []byte(`{"type":"custom_tool_call_output","call_id":"call-1","output":[]}`)}},
	} {
		t.Run(test.name, func(t *testing.T) {
			state := &Context{}
			records := make([]json.RawMessage, len(test.records))
			for index, record := range test.records {
				records[index] = record
			}
			if err := state.AppendRecords(records); err == nil {
				t.Fatalf("accepted invalid call/output pairing: %s", test.name)
			}
			if len(state.Items) != 0 {
				t.Fatalf("failed pairing check partially mutated context: %+v", state.Items)
			}
		})
	}
	state := &Context{}
	if err := state.AppendRecords([]json.RawMessage{call}); err != nil {
		t.Fatal(err)
	}
	if err := state.CheckSettled(); err == nil {
		t.Fatal("unanswered call was considered settled")
	}
}

func TestContextTerminalAndAdapterOnlyState(t *testing.T) {
	state := &Context{}
	if err := state.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: "question"}}); err != nil {
		t.Fatal(err)
	}
	if state.TurnComplete() {
		t.Fatal("user message completed a turn")
	}
	if err := state.AppendNotice([]protocol.ContentPart{{Type: "input_text", Text: "recovery notice"}}); err != nil {
		t.Fatal(err)
	}
	if state.Items[1].Type != ItemMessage || state.Items[1].Role != "developer" || state.TurnComplete() {
		t.Fatalf("notice did not become a non-terminal developer message: %+v", state.Items[1])
	}
	if err := state.ChangeEffort("high"); err != nil {
		t.Fatal(err)
	}
	if err := state.AppendExecutionState(&protocol.ExecutionState{Cells: []protocol.ActiveCell{{ID: "live"}}}); err != nil {
		t.Fatal(err)
	}
	if err := state.AppendRecords([]json.RawMessage{[]byte(`{"type":"message","role":"assistant","content":[]}`)}); err != nil {
		t.Fatal(err)
	}
	if !state.TurnComplete() {
		t.Fatal("terminal assistant message did not complete the settled turn")
	}
	encoded, err := json.Marshal(state)
	if err != nil {
		t.Fatal(err)
	}
	for _, marker := range []string{"anthropic_message", "reasoning_effort", "execution_state", "additional_tools", "compaction_trigger"} {
		if strings.Contains(string(encoded), marker) {
			t.Fatalf("Responses context replayed adapter marker %q: %s", marker, encoded)
		}
	}
}

func TestItemRetainsImageAndAudioContent(t *testing.T) {
	input := []byte(`[{"type":"input_image","image_url":"data:image/png;base64,opaque","detail":"high"},{"type":"input_audio","data":"opaque-audio","format":"wav"}]`)
	var content []protocol.ContentPart
	if err := json.Unmarshal(input, &content); err != nil {
		t.Fatal(err)
	}
	item := Item{Type: ItemCustomToolOutput, CallID: "call-1", Output: content}
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
