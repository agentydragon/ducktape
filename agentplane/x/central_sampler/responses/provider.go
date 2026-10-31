package responses

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
)

const maxProviderErrorBytes = 1024 * 1024

const codexReasoningSummary = "auto"

type RequestParams struct {
	Model          string
	Effort         string
	Instructions   *string
	Attribution    protocol.SessionAttribution
	Items          []Item
	ExecutionState *protocol.ExecutionState
	Tools          protocol.ToolSurface
}

func BuildRequest(params RequestParams, config Config, encoding protocol.ExecEncoding) (Request, error) {
	if err := config.Validate(); err != nil {
		return Request{}, err
	}
	if encoding != protocol.ExecEncodingFreeform && encoding != protocol.ExecEncodingJSON {
		return Request{}, fmt.Errorf("unsupported exec encoding %q: %w", encoding, protocol.ErrInvalid)
	}
	tools, err := toolDefinitions(params.Tools, encoding)
	if err != nil {
		return Request{}, err
	}
	input := make([]RequestInput, 0, len(params.Items)+3)
	if config.Dialect == DialectLite {
		input = append(input, Item{Type: ItemAdditionalTools, Role: "developer", Tools: tools})
	}
	if params.Instructions != nil && *params.Instructions != "" {
		item, err := Message("developer", *params.Instructions)
		if err != nil {
			return Request{}, err
		}
		input = append(input, item)
	}
	if params.ExecutionState != nil && (len(params.ExecutionState.Processes) > 0 || len(params.ExecutionState.Cells) > 0) {
		cells := make([]activeCell, 0, len(params.ExecutionState.Cells))
		for _, cell := range params.ExecutionState.Cells {
			cells = append(cells, activeCell{CellID: cell.ID, Status: cell.Status})
		}
		processes := make([]activeProcess, 0, len(params.ExecutionState.Processes))
		for _, process := range params.ExecutionState.Processes {
			if process.ModelSessionID == nil {
				continue
			}
			processes = append(processes, activeProcess{
				SessionID: *process.ModelSessionID, Status: process.Status, TTY: process.TTY, ExitCode: process.ExitCode,
			})
		}
		state, err := json.Marshal(executionState{Cells: cells, Processes: processes})
		if err != nil {
			return Request{}, err
		}
		item, err := Message("developer", "Active execution handles: "+string(state))
		if err != nil {
			return Request{}, err
		}
		input = append(input, item)
	}
	adapted, err := buildInput(params.Items, config, encoding)
	if err != nil {
		return Request{}, err
	}
	input = append(input, adapted...)
	body := Request{
		Model: params.Model, Instructions: protocol.BaseInstructions, Input: input, Stream: true, Store: false,
		PromptCacheKey: params.Attribution.SessionID, ParallelToolCalls: false,
	}
	if config.Dialect == DialectLite {
		body.Reasoning = &ReasoningConfig{Effort: params.Effort, Summary: codexReasoningSummary, Context: "all_turns"}
		body.Include = []string{"reasoning.encrypted_content"}
		body.ClientMetadata = &ClientMetadata{SessionID: params.Attribution.SessionID, ThreadID: params.Attribution.ThreadID, TurnID: params.Attribution.TurnID}
	} else {
		body.Tools = tools
		if params.Effort != "" {
			body.Reasoning = &ReasoningConfig{Effort: params.Effort}
			body.Include = []string{"reasoning.encrypted_content"}
		}
	}
	return body, nil
}

func toolDefinitions(surface protocol.ToolSurface, encoding protocol.ExecEncoding) ([]AdditionalTool, error) {
	if encoding == protocol.ExecEncodingJSON {
		if !json.Valid(surface.Parameters) {
			return nil, fmt.Errorf("invalid exec tool parameters: %w", protocol.ErrInvalid)
		}
		strict := false
		return []AdditionalTool{{
			Type: "function", Name: "exec", Description: surface.Description, Strict: &strict,
			Parameters: protocol.CloneJSON(surface.Parameters),
		}}, nil
	}
	if !json.Valid(surface.WaitParameters) {
		return nil, fmt.Errorf("invalid wait tool parameters: %w", protocol.ErrInvalid)
	}
	strict := false
	return []AdditionalTool{
		{Type: "custom", Name: "exec", Description: surface.Description, Format: &ToolFormat{Type: "grammar", Syntax: "lark", Definition: surface.FreeformGrammar}},
		{Type: "function", Name: "wait", Description: surface.WaitDescription, Strict: &strict, Parameters: protocol.CloneJSON(surface.WaitParameters)},
	}, nil
}

func BuildCompactionRequest(params RequestParams, config Config, encoding protocol.ExecEncoding) (Request, error) {
	if config.Dialect != DialectLite {
		return Request{}, &protocol.KnownFailureError{Evidence: map[string]any{"reason": "compaction requires a Responses Lite route; standard Responses compaction is not supported"}}
	}
	body, err := BuildRequest(params, config, encoding)
	if err != nil {
		return Request{}, err
	}
	body.Input = append(body.Input, Item{Type: ItemCompactionTrigger})
	return body, nil
}

func buildInput(items []Item, config Config, encoding protocol.ExecEncoding) ([]RequestInput, error) {
	input := make([]RequestInput, 0, len(items))
	answered := map[string]bool{}
	adaptNotifications := config.Dialect == DialectStandard || encoding == protocol.ExecEncodingJSON
	for _, item := range items {
		if adaptNotifications && item.IsToolOutput() {
			if answered[item.CallID] {
				texts := make([]string, 0, len(item.Output))
				for _, part := range item.Output {
					texts = append(texts, part.Text)
				}
				message, err := Message("developer", "Asynchronous output from exec call "+item.CallID+":\n"+strings.Join(texts, "\n"))
				if err != nil {
					return nil, err
				}
				input = append(input, message)
				continue
			}
			answered[item.CallID] = true
		}
		if config.Dialect == DialectStandard && item.Type == ItemFunctionOutput {
			texts := make([]string, 0, len(item.Output))
			for _, part := range item.Output {
				if part.Type != "input_text" {
					break
				}
				texts = append(texts, part.Text)
			}
			if len(texts) == len(item.Output) {
				input = append(input, TextToolOutput{Type: item.Type, ID: item.ID, CallID: item.CallID, Output: strings.Join(texts, "\n")})
				continue
			}
		}
		input = append(input, item)
	}
	return input, nil
}

type executionState struct {
	Cells     []activeCell    `json:"cells"`
	Processes []activeProcess `json:"processes"`
}

type activeCell struct {
	CellID string              `json:"cell_id"`
	Status protocol.CellStatus `json:"status"`
}

type activeProcess struct {
	SessionID int                    `json:"session_id"`
	Status    protocol.ProcessStatus `json:"status"`
	TTY       bool                   `json:"tty"`
	ExitCode  *int                   `json:"exit_code"`
}

// Headers returns the exact non-authentication headers sent for this wire request.
func Headers(config Config, compaction bool) map[string]string {
	headers := map[string]string{"content-type": "application/json"}
	if config.Dialect == DialectLite {
		headers["x-openai-internal-codex-responses-lite"] = "true"
	}
	if compaction {
		headers["x-codex-beta-features"] = "remote_compaction_v2"
	}
	return headers
}

func Sample(ctx context.Context, client *http.Client, baseURL, credential string, body Request, config Config,
	evidence protocol.EvidenceSink, attribution map[string]string, compaction bool,
) (*Response, error) {
	encoded, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(baseURL, "/")+"/v1/responses", bytes.NewReader(encoded))
	if err != nil {
		return nil, err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+credential)
	for name, value := range Headers(config, compaction) {
		if name != "content-type" {
			request.Header.Set(name, value)
		}
	}
	for name, value := range attribution {
		request.Header.Set(name, value)
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	reader := protocol.CaptureReader{Reader: response.Body, Context: ctx, Evidence: evidence}
	providerRequestID := response.Header.Get("x-request-id")
	if evidence != nil {
		if err := evidence.Record(ctx, protocol.HTTPResponseHead{
			HTTPStatus: response.StatusCode, ProviderRequestID: providerRequestID, ContentType: response.Header.Get("content-type"),
		}); err != nil {
			return nil, err
		}
	}
	if response.StatusCode >= http.StatusBadRequest {
		body, readErr := io.ReadAll(io.LimitReader(reader, protocol.MaxProviderStreamBytes))
		if readErr != nil {
			return nil, readErr
		}
		if len(body) > maxProviderErrorBytes {
			body = append(body[:maxProviderErrorBytes], []byte("[truncated]")...)
		}
		return nil, &protocol.KnownFailureError{Evidence: map[string]any{"http_status": response.StatusCode, "body": string(body)}}
	}
	result, err := readStream(ctx, io.LimitReader(reader, protocol.MaxProviderStreamBytes), evidence)
	if err != nil {
		return nil, err
	}
	return result, nil
}

func readStream(ctx context.Context, reader io.Reader, evidence protocol.EvidenceSink) (*Response, error) {
	result := &Response{}
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
			payload, err := eventPayload(event)
			if err != nil {
				return err
			}
			observations, err := Observations(event)
			if err != nil {
				return err
			}
			if err := evidence.Record(ctx, payload, observations...); err != nil {
				return err
			}
		}
		result.Events = append(result.Events, event)
		if event.Type == EventOutputItemDone {
			if event.Item == nil {
				return errors.New("provider output item event has no item object")
			}
			result.Output = append(result.Output, *event.Item)
		}
		if event.Type == EventCompleted {
			if event.Response == nil {
				return &protocol.KnownFailureError{Evidence: map[string]any{"reason": "completion event has no response object", "events": result.Events}}
			}
			if status, present := event.Response.StatusValue(); present && status != "completed" {
				return &protocol.KnownFailureError{Evidence: map[string]any{"reason": "unexpected terminal status", "response": event.Response, "events": result.Events}}
			}
			// Like Codex, consume output_item.done items. Antigravity through LiteLLM can
			// rewrite reasoning and add items only in terminal output.
			result.Terminal = event.Response
			return errProviderComplete
		}
		if event.Type == EventFailed || event.Type == EventIncomplete || event.Type == EventError {
			return &protocol.KnownFailureError{Evidence: map[string]any{"terminal": event, "events": result.Events}}
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

func decodeProviderEvent(data string) (Event, error) {
	var event Event
	if err := json.Unmarshal([]byte(data), &event); err != nil {
		return Event{}, fmt.Errorf("decode provider SSE event: %w", err)
	}
	return event, nil
}

// streamEvent is the evidence of one Responses SSE event, recorded in its canonical JSON form.
type streamEvent struct{ value any }

func (streamEvent) Kind() protocol.ProviderEvidenceKind { return protocol.EvidenceResponsesEvent }

func (event streamEvent) MarshalJSON() ([]byte, error) { return json.Marshal(event.value) }

func eventPayload(event Event) (streamEvent, error) {
	data := event.Raw
	if len(data) == 0 {
		var err error
		data, err = json.Marshal(event)
		if err != nil {
			return streamEvent{}, err
		}
	}
	value, err := protocol.DecodeJSONValue(data)
	if err != nil {
		return streamEvent{}, err
	}
	if _, ok := value.(map[string]any); !ok {
		return streamEvent{}, fmt.Errorf("provider event must be an object: %w", protocol.ErrInvalid)
	}
	return streamEvent{value: value}, nil
}
