package centralsampler

import (
	"bufio"
	"bytes"
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
)

const (
	maxProviderErrorBytes  = 1024 * 1024
	maxProviderStreamBytes = 64 * 1024 * 1024
)

// Capture transport bytes before parsing, including a final incomplete SSE event. JSON encodes
// []byte as base64, preserving UTF-8 codepoints split across network reads without replacement.
type providerCaptureReader struct {
	reader   io.Reader
	ctx      context.Context
	evidence *ResponseEvidence
}

func (r providerCaptureReader) Read(buffer []byte) (int, error) {
	n, readErr := r.reader.Read(buffer)
	if n > 0 && r.evidence != nil {
		if err := r.evidence.Store.ProtocolEvent(r.ctx, r.evidence.Owner, r.evidence.IntentID, "provider_wire", "received", map[string]any{"bytes": buffer[:n]}); err != nil {
			return 0, err
		}
	}
	return n, readErr
}

// Codex rust-v0.162.1 codex-rs/models-manager/models.json: gpt-6.1-sol
// model_messages.instructions_template, used for every model. Pre-commit trims trailing whitespace.
// Re-copy it on Codex pin bumps.
// TODO: Consider pruning or rewriting it for the sampler's tool surface, or choosing it per model
// as Codex does.
//
//go:embed codex/base_instructions.txt
var baseInstructions string

type ResponseEvidence struct {
	Store    *Store
	Owner    Owner
	IntentID string
}

type ResponsesResponse struct {
	Terminal          *ResponsesTerminal
	Output            []ResponsesItem
	Events            []ResponsesEvent
	HTTPStatus        int
	ProviderRequestID string
}

func (r ResponsesResponse) Audit() map[string]any {
	return map[string]any{"response": r.Terminal, "events": r.Events}
}

// TODO: Codex also nests update_plan and MCP tools (namespaced, e.g. via the Action Service);
// consider exposing them here.
func toolDefinitions() []ResponsesCodeToolDefinition {
	return []ResponsesCodeToolDefinition{
		execCommandToolDefinition(),
		writeStdinToolDefinition(),
		{
			Name: "apply_patch", ToolName: ResponsesToolName{Name: "apply_patch"},
			Description: "Apply a Codex *** Begin Patch / *** End Patch patch through upstream apply-patch.",
			Kind:        "freeform",
		},
		viewImageToolDefinition(),
		// TODO: update_plan and Action-backed (MCP) tools, which Codex also nests.
	}
}

// ResponsesRequestParams contains the runtime inputs needed to build a complete wire request.
// It is not serialized to the upstream API.
type ResponsesRequestParams struct {
	Model          string
	Effort         string
	Attribution    SessionAttribution
	Items          []ResponsesItem
	Instructions   *string
	ExecutionState *ExecutionState
}

func BuildResponsesRequest(params ResponsesRequestParams, config ModelConfig) (ResponsesRequest, error) {
	if err := config.Validate(); err != nil {
		return ResponsesRequest{}, err
	}
	description, err := execToolDescription(toolDefinitions(), config)
	if err != nil {
		return ResponsesRequest{}, err
	}
	var tools []ResponsesAdditionalTool
	if config.ExecEncoding == ExecEncodingJSON {
		tool, err := jsonExecToolDefinition(description)
		if err != nil {
			return ResponsesRequest{}, err
		}
		tools = []ResponsesAdditionalTool{tool}
	} else {
		strict := false
		tools = []ResponsesAdditionalTool{
			// Codex rust-v0.162.1 core/src/tools/code_mode/execute_spec.rs create_code_mode_tool.
			{Type: "custom", Name: "exec", Description: description, Format: &ResponsesToolFormat{Type: "grammar", Syntax: "lark", Definition: execFreeformGrammar}},
			{Type: "function", Name: "wait", Description: waitToolDescription(), Strict: &strict, Parameters: waitToolParameters()},
		}
	}
	input := make([]ResponsesInput, 0, len(params.Items)+3)
	if config.Responses.Dialect == ResponsesDialectLite {
		input = append(input, ResponsesItem{Type: ResponsesItemAdditionalTools, Role: "developer", Tools: tools})
	}
	if params.Instructions != nil && *params.Instructions != "" {
		item, err := newResponsesMessage("developer", *params.Instructions)
		if err != nil {
			return ResponsesRequest{}, err
		}
		input = append(input, item)
	}
	if params.ExecutionState != nil && (len(params.ExecutionState.Processes) > 0 || len(params.ExecutionState.Cells) > 0) {
		cells := make([]ResponsesActiveCell, 0, len(params.ExecutionState.Cells))
		for _, cell := range params.ExecutionState.Cells {
			cells = append(cells, ResponsesActiveCell{CellID: cell.ID, Status: cell.Status})
		}
		processes := make([]ResponsesActiveProcess, 0, len(params.ExecutionState.Processes))
		for _, process := range params.ExecutionState.Processes {
			if process.ModelSessionID == nil {
				continue
			}
			processes = append(processes, ResponsesActiveProcess{
				SessionID: *process.ModelSessionID, Status: process.Status, TTY: process.TTY, ExitCode: process.ExitCode,
			})
		}
		state, err := json.Marshal(ResponsesExecutionState{Cells: cells, Processes: processes})
		if err != nil {
			return ResponsesRequest{}, err
		}
		item, err := newResponsesMessage("developer", "Active execution handles: "+string(state))
		if err != nil {
			return ResponsesRequest{}, err
		}
		input = append(input, item)
	}
	adapted, err := buildResponsesInput(params.Items, config)
	if err != nil {
		return ResponsesRequest{}, err
	}
	input = append(input, adapted...)
	body := ResponsesRequest{
		Model: params.Model, Instructions: baseInstructions, Input: input, Stream: true, Store: false,
		PromptCacheKey: params.Attribution.SessionID,
	}
	if config.Responses.Dialect == ResponsesDialectLite {
		body.Reasoning = &ResponsesReasoningConfig{Effort: params.Effort, Summary: codexReasoningSummary, Context: "all_turns"}
		body.Include = []string{"reasoning.encrypted_content"}
		body.ClientMetadata = &ResponsesClientMetadata{SessionID: params.Attribution.SessionID, ThreadID: params.Attribution.ThreadID, TurnID: params.Attribution.TurnID}
	} else {
		body.Tools = tools
		if params.Effort != "" {
			body.Reasoning = &ResponsesReasoningConfig{Effort: params.Effort}
			body.Include = []string{"reasoning.encrypted_content"}
		}
	}
	return body, nil
}

func BuildResponsesCompactionRequest(params ResponsesRequestParams, config ModelConfig) (ResponsesRequest, error) {
	if config.Responses.Dialect != ResponsesDialectLite {
		return ResponsesRequest{}, &KnownFailureError{Evidence: map[string]any{"reason": "compaction requires a Responses Lite route; standard Responses compaction is not supported"}}
	}
	body, err := BuildResponsesRequest(params, config)
	if err != nil {
		return ResponsesRequest{}, err
	}
	body.Input = append(body.Input, ResponsesItem{Type: ResponsesItemCompactionTrigger})
	return body, nil
}

// buildResponsesInput adapts durable observations before the wire request is serialized.
// It retains native items, and converts notifications and public text tool results.
func buildResponsesInput(items []ResponsesItem, config ModelConfig) ([]ResponsesInput, error) {
	input := make([]ResponsesInput, 0, len(items))
	answered := map[string]bool{}
	adaptNotifications := config.Responses.Dialect == ResponsesDialectStandard || config.ExecEncoding == ExecEncodingJSON
	for _, item := range items {
		if adaptNotifications && item.IsToolOutput() {
			if answered[item.CallID] {
				texts := make([]string, 0, len(item.Output))
				for _, part := range item.Output {
					texts = append(texts, part.Text)
				}
				message, err := newResponsesMessage("developer", "Asynchronous output from exec call "+item.CallID+":\n"+strings.Join(texts, "\n"))
				if err != nil {
					return nil, err
				}
				input = append(input, message)
				continue
			}
			answered[item.CallID] = true
		}
		if config.Responses.Dialect == ResponsesDialectStandard && item.Type == ResponsesItemFunctionOutput {
			texts := make([]string, 0, len(item.Output))
			for _, part := range item.Output {
				if part.Type != "input_text" {
					break
				}
				texts = append(texts, part.Text)
			}
			if len(texts) == len(item.Output) {
				input = append(input, ResponsesTextToolOutput{Type: item.Type, ID: item.ID, CallID: item.CallID, Output: strings.Join(texts, "\n")})
				continue
			}
		}
		input = append(input, item)
	}
	return input, nil
}

type ResponsesExecutionState struct {
	Cells     []ResponsesActiveCell    `json:"cells"`
	Processes []ResponsesActiveProcess `json:"processes"`
}

type ResponsesActiveCell struct {
	CellID string `json:"cell_id"`
	Status string `json:"status"`
}

type ResponsesActiveProcess struct {
	SessionID int    `json:"session_id"`
	Status    string `json:"status"`
	TTY       bool   `json:"tty"`
	ExitCode  *int   `json:"exit_code"`
}

func newResponsesMessage(role, content string) (ResponsesItem, error) {
	encoded, err := json.Marshal(content)
	if err != nil {
		return ResponsesItem{}, err
	}
	return ResponsesItem{Type: ResponsesItemMessage, Role: role, Content: encoded}, nil
}

func canonicalJSON(value any) string {
	data, err := json.Marshal(value)
	if err != nil {
		return "{}"
	}
	return string(data)
}

func Sample(ctx context.Context, client *http.Client, proxyURL, credential string, body ResponsesRequest, responses ResponsesConfig, evidence *ResponseEvidence, attribution map[string]string, compaction bool) (*ResponsesResponse, error) {
	encoded, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(proxyURL, "/")+"/v1/responses", bytes.NewReader(encoded))
	if err != nil {
		return nil, err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+credential)
	if responses.Dialect == ResponsesDialectLite {
		request.Header.Set("x-openai-internal-codex-responses-lite", "true")
	}
	for name, value := range attribution {
		request.Header.Set(name, value)
	}
	if compaction {
		request.Header.Set("x-codex-beta-features", "remote_compaction_v2")
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	reader := providerCaptureReader{reader: response.Body, ctx: ctx, evidence: evidence}
	if evidence != nil {
		if err := evidence.Store.ProtocolEvent(ctx, evidence.Owner, evidence.IntentID, "provider_http", "received", map[string]any{"http_status": response.StatusCode, "provider_request_id": response.Header.Get("x-request-id"), "content_type": response.Header.Get("content-type")}); err != nil {
			return nil, err
		}
	}
	if response.StatusCode >= http.StatusBadRequest {
		body, readErr := io.ReadAll(io.LimitReader(reader, maxProviderStreamBytes))
		if readErr != nil {
			return nil, readErr
		}
		if len(body) > maxProviderErrorBytes {
			body = append(body[:maxProviderErrorBytes], []byte("[truncated]")...)
		}
		return nil, &KnownFailureError{Evidence: map[string]any{"http_status": response.StatusCode, "body": string(body)}}
	}
	return readProviderStream(ctx, io.LimitReader(reader, maxProviderStreamBytes), evidence)
}

func readProviderStream(ctx context.Context, reader io.Reader, evidence *ResponseEvidence) (*ResponsesResponse, error) {
	result := &ResponsesResponse{}
	lineReader := bufio.NewReader(reader)
	var dataLines []string
	dispatch := func() error {
		if len(dataLines) == 0 {
			return nil
		}
		event, err := decodeProviderEvent(strings.Join(dataLines, "\n"))
		dataLines = nil
		if err != nil {
			return err
		}
		if evidence != nil {
			if err := evidence.Store.ProviderEvent(ctx, evidence.Owner, evidence.IntentID, event); err != nil {
				return err
			}
		}
		result.Events = append(result.Events, event)
		if event.Type == ResponsesEventOutputItemDone {
			if event.Item == nil {
				return errors.New("provider output item event has no item object")
			}
			result.Output = append(result.Output, *event.Item)
		}
		if event.Type == ResponsesEventCompleted {
			if event.Response == nil {
				return &KnownFailureError{Evidence: map[string]any{"reason": "completion event has no response object", "events": result.Events}}
			}
			if status, present := event.Response.StatusValue(); present && status != "completed" {
				return &KnownFailureError{Evidence: map[string]any{"reason": "unexpected terminal status", "response": event.Response, "events": result.Events}}
			}
			// Like Codex, consume output_item.done items. Antigravity through
			// LiteLLM can rewrite reasoning and add items only in terminal output.
			result.Terminal = event.Response
			return errProviderComplete
		}
		if event.Type == ResponsesEventFailed || event.Type == ResponsesEventIncomplete || event.Type == ResponsesEventError {
			return &KnownFailureError{Evidence: map[string]any{"terminal": event, "events": result.Events}}
		}
		return nil
	}
	for {
		line, err := lineReader.ReadString('\n')
		if len(line) > 0 {
			line = strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
			if strings.HasPrefix(line, "data:") {
				dataLines = append(dataLines, strings.TrimLeft(line[5:], " \t"))
			} else if line == "" {
				if dispatchErr := dispatch(); dispatchErr != nil {
					if errors.Is(dispatchErr, errProviderComplete) {
						return result, nil
					}
					return nil, dispatchErr
				}
			}
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			return nil, err
		}
	}
	return nil, errors.New("provider stream ended without response.completed; sampling outcome unknown")
}

var errProviderComplete = errors.New("provider response complete")

func decodeProviderEvent(data string) (ResponsesEvent, error) {
	var event ResponsesEvent
	if err := json.Unmarshal([]byte(data), &event); err != nil {
		return ResponsesEvent{}, fmt.Errorf("decode provider SSE event: %w", err)
	}
	return event, nil
}

func equalJSON(left, right any) bool { return bytes.Equal(mustJSON(left), mustJSON(right)) }

func mustJSON(value any) []byte {
	data, _ := json.Marshal(value)
	return data
}

// Authentication is deliberately absent. This record is committed in the same transaction as
// the pending intent, before any upstream request can be sent.
func providerRequestAudit(kind string, connection ConnectionConfig, config ModelConfig, body any, wire []byte) map[string]any {
	headers := map[string]string{"content-type": "application/json"}
	path := "/v1/responses"
	if config.API == APIStyleAnthropicMessages {
		path = "/v1/messages"
		headers["anthropic-version"] = "2023-06-01"
		if betas := anthropicBetas(config); betas != "" {
			headers["anthropic-beta"] = betas
		}
	} else {
		if config.Responses.Dialect == ResponsesDialectLite {
			headers["x-openai-internal-codex-responses-lite"] = "true"
		}
		if kind == "compaction" {
			headers["x-codex-beta-features"] = "remote_compaction_v2"
		}
	}
	return map[string]any{
		"kind": kind, "api": config.API, "connection": config.Connection, "provider_model": config.ProviderModel,
		"method": "POST", "url": strings.TrimRight(connection.BaseURL, "/") + path, "headers": headers, "body": body, "wire_request": string(wire),
	}
}
