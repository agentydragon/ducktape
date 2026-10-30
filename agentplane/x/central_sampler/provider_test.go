package centralsampler

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
)

func TestCompletedItemTextJoinsReasoningSummaryParts(t *testing.T) {
	for _, test := range []struct {
		item string
		want string
	}{
		{`{"type":"reasoning","summary":[{"type":"summary_text","text":"**Plan**"},{"type":"summary_text","text":"Read the file."}]}`, "**Plan**\nRead the file."},
		{`{"type":"message","role":"assistant","content":[{"type":"output_text","text":"one "},{"type":"output_text","text":"answer"}]}`, "one answer"},
	} {
		var item ResponsesItem
		if err := json.Unmarshal([]byte(test.item), &item); err != nil {
			t.Fatal(err)
		}
		got, err := responsesText(item)
		if err != nil {
			t.Fatal(err)
		}
		if got != test.want {
			t.Fatalf("responsesText(%s) = %q, want %q", test.item, got, test.want)
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
	response, err := readProviderStream(context.Background(), strings.NewReader(stream), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(response.Output) != 2 {
		t.Fatalf("expected only streamed completed items, got %s", canonicalJSON(response.Output))
	}
	reasoning, call := response.Output[0], response.Output[1]
	text, err := responsesText(reasoning)
	if err != nil {
		t.Fatal(err)
	}
	if reasoning.Type != ResponsesItemReasoning || text != "Inspect the workspace." || reasoning.EncryptedContent != "" || len(reasoning.Content) != 0 {
		t.Fatalf("streamed reasoning was replaced: %s", canonicalJSON(reasoning))
	}
	if call.Type != ResponsesItemFunctionCall || call.CallID != "call_fixture" || call.Name != "exec" || call.Arguments != `{"code":"text(1)"}` {
		t.Fatalf("streamed tool call changed: %s", canonicalJSON(call))
	}
	var terminal struct {
		Output []ResponsesItem `json:"output"`
	}
	if err := json.Unmarshal([]byte(canonicalJSON(response.Terminal)), &terminal); err != nil {
		t.Fatal(err)
	}
	if len(terminal.Output) != 3 || terminal.Output[0].EncryptedContent != "fixture-opaque-reasoning" {
		t.Fatalf("terminal evidence was not retained: %s", canonicalJSON(response.Terminal))
	}
}

func TestResponsesRequestUsesCodexToolsAndPreservesOpaqueContext(t *testing.T) {
	var item ResponsesItem
	if err := json.Unmarshal([]byte(`{"type":"reasoning","encrypted_content":"opaque","provider_sequence":9007199254740993}`), &item); err != nil {
		t.Fatal(err)
	}
	body, err := BuildResponsesRequest(ResponsesRequestParams{Model: "fixture-model", Attribution: SessionAttribution{SessionID: "fixture-session"}, Items: []ResponsesItem{item}}, ModelConfig{Connection: "provider", ProviderModel: "fixture-model", API: APIStyleResponses, Responses: ResponsesConfig{Dialect: ResponsesDialectLite}, ExecEncoding: ExecEncodingFreeform, ReasoningEfforts: []string{"", "high"}})
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
	for _, tool := range body.Input[0].(ResponsesItem).Tools {
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
	for _, dialect := range []ResponsesDialect{ResponsesDialectLite, ResponsesDialectStandard} {
		for _, effort := range []string{"", "high"} {
			t.Run(string(dialect)+"/"+effort, func(t *testing.T) {
				attribution := SessionAttribution{SessionID: "session", ThreadID: "thread", TurnID: "turn"}
				params := ResponsesRequestParams{Model: "fixture", Effort: effort, Attribution: attribution}
				config := ModelConfig{Connection: "provider", ProviderModel: "fixture-model", API: APIStyleResponses, Responses: ResponsesConfig{Dialect: dialect}, ExecEncoding: ExecEncodingJSON, ReasoningEfforts: []string{"", "high"}}
				body, err := BuildResponsesRequest(params, config)
				if err != nil {
					t.Fatal(err)
				}
				if body.PromptCacheKey != attribution.SessionID {
					t.Fatalf("prompt cache key = %q", body.PromptCacheKey)
				}
				if dialect == ResponsesDialectLite {
					want := ResponsesClientMetadata{SessionID: "session", ThreadID: "thread", TurnID: "turn"}
					if body.ClientMetadata == nil || *body.ClientMetadata != want {
						t.Fatalf("client metadata = %+v", body.ClientMetadata)
					}
				} else if body.ClientMetadata != nil {
					t.Fatal("standard request includes private metadata")
				}
				if dialect == ResponsesDialectLite || effort != "" {
					if body.Reasoning == nil || body.Reasoning.Effort != effort || !slices.Equal(body.Include, []string{"reasoning.encrypted_content"}) {
						t.Fatalf("reasoning = %+v, include = %v", body.Reasoning, body.Include)
					}
					wantContext, wantSummary := "", ""
					if dialect == ResponsesDialectLite {
						wantContext, wantSummary = "all_turns", codexReasoningSummary
					}
					if body.Reasoning.Context != wantContext || body.Reasoning.Summary != wantSummary {
						t.Fatalf("reasoning dialect settings = %+v", body.Reasoning)
					}
				} else if body.Reasoning != nil || len(body.Include) != 0 {
					t.Fatal("standard request without effort includes reasoning configuration")
				}
				compaction, err := BuildResponsesCompactionRequest(params, config)
				if dialect == ResponsesDialectStandard {
					if err == nil {
						t.Fatal("standard compaction accepted")
					}
				} else {
					if err != nil {
						t.Fatal(err)
					}
					if compaction.Input[len(compaction.Input)-1].(ResponsesItem).Type != ResponsesItemCompactionTrigger || *compaction.ClientMetadata != *body.ClientMetadata {
						t.Fatalf("compaction request = %+v", compaction)
					}
				}
			})
		}
	}
}

func TestResponsesDialectAndExecEncodingAreIndependent(t *testing.T) {
	for _, mode := range []ResponsesDialect{ResponsesDialectStandard, ResponsesDialectLite} {
		for _, encoding := range []ExecEncoding{ExecEncodingJSON, ExecEncodingFreeform} {
			t.Run(string(mode)+"/"+string(encoding), func(t *testing.T) {
				config := ModelConfig{Connection: "provider", ProviderModel: "fixture-model", API: APIStyleResponses, Responses: ResponsesConfig{Dialect: mode}, ExecEncoding: encoding, ReasoningEfforts: []string{"", "high"}}
				body, err := BuildResponsesRequest(ResponsesRequestParams{Model: "fixture", Attribution: SessionAttribution{SessionID: "session"}}, config)
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
				if mode == ResponsesDialectLite {
					tools = body.Input[0].(ResponsesItem).Tools
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
				if strings.Contains(execDescription, "// @exec:") != (encoding == ExecEncodingFreeform) {
					t.Fatal("exec description has wrong input contract")
				}
				if encoding == ExecEncodingJSON {
					if len(tools) != 1 || tools[0].Name != "exec" || tools[0].Type != "function" {
						t.Fatalf("JSON tools: %+v", tools)
					}
					if strings.Contains(tools[0].Description, "Accepts raw JavaScript") {
						t.Fatal("JSON exec advertises raw input")
					}
				} else if len(tools) != 2 || tools[0].Type != "custom" || tools[1].Name != "wait" {
					t.Fatalf("freeform tools: %+v", tools)
				}
				for _, call := range []ToolInvocation{
					{Encoding: ExecEncodingJSON, Name: "exec"},
					{Encoding: ExecEncodingFreeform, Name: "exec"},
					{Encoding: ExecEncodingJSON, Name: "wait"},
				} {
					want := encoding == ExecEncodingJSON && call.Name == "exec" && call.Encoding == ExecEncodingJSON ||
						encoding == ExecEncodingFreeform && (call.Name == "exec" && call.Encoding == ExecEncodingFreeform || call.Name == "wait" && call.Encoding == ExecEncodingJSON)
					if call.IsModelTool(config) != want {
						t.Fatalf("wrong tool validation: %+v", call)
					}
					wantConfiguredName := call.Name == "exec" || encoding == ExecEncodingFreeform && call.Name == "wait"
					if call.IsConfiguredToolName(config) != wantConfiguredName {
						t.Fatalf("wrong configured-name validation: %+v", call)
					}
				}
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.URL.Path != "/v1/responses" {
						t.Errorf("Responses request path: %q", r.URL.Path)
					}
					if got := r.Header.Get("x-openai-internal-codex-responses-lite"); (got == "true") != (mode == ResponsesDialectLite) {
						t.Errorf("Lite header: %q", got)
					}
					_, _ = w.Write([]byte("data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\",\"output\":[]}}\n\n"))
				}))
				defer server.Close()
				if _, err := Sample(context.Background(), server.Client(), server.URL, "fixture", body, config.Responses, nil, nil, false); err != nil {
					t.Fatal(err)
				}
			})
		}
	}
}

func TestStandardResponsesSerializesTextAndAsyncNotifications(t *testing.T) {
	state := &ResponsesContext{}
	if err := state.AppendRecords([]json.RawMessage{[]byte(`{"type":"function_call","name":"exec","call_id":"call","arguments":"{\"code\":\"text(1)\"}"}`)}); err != nil {
		t.Fatal(err)
	}
	call, err := state.FindCall("call")
	if err != nil {
		t.Fatal(err)
	}
	if err := state.AppendToolResult(call, []ContentPart{{Type: "input_text", Text: "result"}}); err != nil {
		t.Fatal(err)
	}
	if err := state.AppendToolResult(call, []ContentPart{{Type: "input_text", Text: "later"}}); err != nil {
		t.Fatal(err)
	}
	items := state.Items
	body, err := BuildResponsesRequest(ResponsesRequestParams{Model: "fixture", Attribution: SessionAttribution{SessionID: "session"}, Items: items}, ModelConfig{Connection: "provider", ProviderModel: "fixture-model", API: APIStyleResponses, Responses: ResponsesConfig{Dialect: ResponsesDialectStandard}, ExecEncoding: ExecEncodingJSON, ReasoningEfforts: []string{"", "high"}})
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
	if items[1].Type != ResponsesItemFunctionOutput || items[2].Type != ResponsesItemFunctionOutput {
		t.Fatal("async outputs did not retain the function-call encoding")
	}
}

func TestToolInvocationShapeSeparatesUnknownNamesFromMalformedItems(t *testing.T) {
	for _, test := range []struct {
		name         string
		item         string
		wantValid    bool
		wantEncoding ExecEncoding
	}{
		{
			name:         "unknown custom tool has an answerable shape",
			item:         `{"type":"custom_tool_call","call_id":"custom-1","name":"missing_custom","input":"{}"}`,
			wantValid:    true,
			wantEncoding: ExecEncodingFreeform,
		},
		{
			name:         "unknown function has an answerable shape",
			item:         `{"type":"function_call","call_id":"function-1","name":"missing_function","arguments":"{}"}`,
			wantValid:    true,
			wantEncoding: ExecEncodingJSON,
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
			var item ResponsesItem
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

func TestResponsesContextRetainsNativeRawRecordsAndNumericPrecision(t *testing.T) {
	state := &ResponsesContext{}
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

func TestResponsesContextRejectsMarkersAndUnknownRecordsAtomically(t *testing.T) {
	for _, typeName := range []string{
		"anthropic_message", "reasoning_effort", "reasoning_effort_restore", "execution_state",
		"additional_tools", "compaction_trigger", "unknown_record",
	} {
		t.Run(typeName, func(t *testing.T) {
			state := &ResponsesContext{}
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

func TestResponsesContextTracksPendingCallsAndRepeatedOutputs(t *testing.T) {
	state := &ResponsesContext{}
	if err := state.AppendRecords([]json.RawMessage{
		[]byte(`{"type":"message","role":"user","content":[]}`),
		[]byte(`{"type":"function_call","call_id":"call-1","name":"exec","arguments":"{}"}`),
	}); err != nil {
		t.Fatal(err)
	}
	pending, err := state.PendingCalls()
	if err != nil || len(pending) != 1 || pending[0].SourceRecord != 2 || pending[0].Encoding != ExecEncodingJSON {
		t.Fatalf("pending calls=%+v, error=%v", pending, err)
	}
	call, err := state.FindCall("call-1")
	if err != nil || call.SourceRecord != 2 {
		t.Fatalf("found call=%+v, error=%v", call, err)
	}
	if _, err := state.FindCall("missing"); !errors.Is(err, ErrMissing) {
		t.Fatalf("unknown call error=%v, want ErrMissing", err)
	}
	parts := []ContentPart{{Type: "input_text", Text: "first"}}
	if err := state.AppendToolResult(call, parts); err != nil {
		t.Fatal(err)
	}
	if err := state.AppendToolResult(call, []ContentPart{{Type: "input_text", Text: "async"}}); err != nil {
		t.Fatal(err)
	}
	if state.Items[2].Type != ResponsesItemFunctionOutput || state.Items[3].Type != ResponsesItemFunctionOutput {
		t.Fatalf("tool outputs=%+v", state.Items[2:])
	}
	if err := state.CheckSettled(); err != nil {
		t.Fatalf("repeated async output left call unsettled: %v", err)
	}
}

func TestResponsesContextUsesCustomOutputForFreeformCalls(t *testing.T) {
	state := &ResponsesContext{}
	if err := state.AppendRecords([]json.RawMessage{
		[]byte(`{"type":"custom_tool_call","call_id":"exec-1","name":"exec","input":"text(1)"}`),
	}); err != nil {
		t.Fatal(err)
	}
	call, err := state.FindCall("exec-1")
	if err != nil {
		t.Fatal(err)
	}
	if err := state.AppendToolResult(call, []ContentPart{{Type: "input_text", Text: "1"}}); err != nil {
		t.Fatal(err)
	}
	if got := state.Items[1].Type; got != ResponsesItemCustomToolOutput {
		t.Fatalf("freeform result type=%s, want %s", got, ResponsesItemCustomToolOutput)
	}
}

func TestResponsesContextRejectsDuplicateOrOrphanToolRecords(t *testing.T) {
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
			state := &ResponsesContext{}
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
	state := &ResponsesContext{}
	if err := state.AppendRecords([]json.RawMessage{call}); err != nil {
		t.Fatal(err)
	}
	if err := state.CheckSettled(); err == nil {
		t.Fatal("unanswered call was considered settled")
	}
}

func TestResponsesContextTerminalAndAdapterOnlyState(t *testing.T) {
	state := &ResponsesContext{}
	if err := state.AppendUser([]ContentPart{{Type: "input_text", Text: "question"}}); err != nil {
		t.Fatal(err)
	}
	if state.TurnComplete() {
		t.Fatal("user message completed a turn")
	}
	if err := state.AppendNotice([]ContentPart{{Type: "input_text", Text: "recovery notice"}}); err != nil {
		t.Fatal(err)
	}
	if state.Items[1].Type != ResponsesItemMessage || state.Items[1].Role != "developer" || state.TurnComplete() {
		t.Fatalf("notice did not become a non-terminal developer message: %+v", state.Items[1])
	}
	if err := state.ChangeEffort("high"); err != nil {
		t.Fatal(err)
	}
	if err := state.AppendExecutionState(&ExecutionState{Cells: []LiveCell{{ID: "live"}}}); err != nil {
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

func TestResponsesItemRetainsImageAndAudioContent(t *testing.T) {
	input := []byte(`[{"type":"input_image","image_url":"data:image/png;base64,opaque","detail":"high"},{"type":"input_audio","data":"opaque-audio","format":"wav"}]`)
	var content []ContentPart
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
