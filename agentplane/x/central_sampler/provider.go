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

const maxProviderErrorBytes = 1024 * 1024

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

type ProviderResponse struct {
	Terminal *ResponsesTerminal
	Output   []ResponsesItem
	Events   []ResponsesEvent
}

func (r ProviderResponse) Audit() map[string]any {
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

func RequestBody(model, conversationID string, items []ResponsesItem, instructions *string, executionState *ExecutionState) (ResponsesRequest, error) {
	description, err := execToolDescription(toolDefinitions())
	if err != nil {
		return ResponsesRequest{}, err
	}
	strict := false
	input := []ResponsesItem{{
		Type: ResponsesItemAdditionalTools, Role: "developer", Tools: []ResponsesAdditionalTool{
			// Codex rust-v0.162.1 core/src/tools/code_mode/execute_spec.rs create_code_mode_tool.
			{Type: "custom", Name: "exec", Description: description, Format: &ResponsesToolFormat{Type: "grammar", Syntax: "lark", Definition: execFreeformGrammar}},
			{Type: "function", Name: "wait", Description: waitToolDescription(), Strict: &strict, Parameters: waitToolParameters()},
		},
	}}
	if instructions != nil && *instructions != "" {
		item, err := newResponsesMessage("developer", *instructions)
		if err != nil {
			return ResponsesRequest{}, err
		}
		input = append(input, item)
	}
	if executionState != nil && (len(executionState.Processes) > 0 || len(executionState.Cells) > 0) {
		cells := make([]ResponsesActiveCell, 0, len(executionState.Cells))
		for _, cell := range executionState.Cells {
			cells = append(cells, ResponsesActiveCell{CellID: cell.ID, Status: cell.Status})
		}
		processes := make([]ResponsesActiveProcess, 0, len(executionState.Processes))
		for _, process := range executionState.Processes {
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
	input = append(input, items...)
	return ResponsesRequest{
		Model: model, Instructions: baseInstructions, Input: input, Stream: true, Store: false,
		Include: []string{"reasoning.encrypted_content"}, PromptCacheKey: conversationID,
		ClientMetadata: ResponsesClientMetadata{SessionID: conversationID, ThreadID: conversationID, TurnID: conversationID},
	}, nil
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

func Sample(ctx context.Context, client *http.Client, endpoint, credential string, body ResponsesRequest, evidence *ResponseEvidence, attribution map[string]string, compaction bool) (*ProviderResponse, error) {
	encoded, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(encoded))
	if err != nil {
		return nil, err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+credential)
	request.Header.Set("x-openai-internal-codex-responses-lite", "true")
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
	if response.StatusCode >= http.StatusBadRequest {
		body, readErr := io.ReadAll(io.LimitReader(response.Body, maxProviderErrorBytes+1))
		if readErr != nil {
			return nil, readErr
		}
		if len(body) > maxProviderErrorBytes {
			body = append(body[:maxProviderErrorBytes], []byte("[truncated]")...)
		}
		return nil, &KnownFailureError{Evidence: map[string]any{"http_status": response.StatusCode, "body": string(body)}}
	}
	return readProviderStream(ctx, response.Body, evidence)
}

func readProviderStream(ctx context.Context, reader io.Reader, evidence *ResponseEvidence) (*ProviderResponse, error) {
	result := &ProviderResponse{}
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
			terminalOutput, present, parseErr := event.Response.OutputItems()
			if parseErr != nil {
				return &KnownFailureError{Evidence: map[string]any{"reason": "terminal output is not an array of items", "events": result.Events}}
			}
			if present {
				if len(result.Output) > 0 && !equalResponsesItems(result.Output, terminalOutput) {
					return &KnownFailureError{Evidence: map[string]any{"reason": "terminal output disagrees with streamed completed items", "events": result.Events}}
				}
				result.Output = terminalOutput
			}
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
