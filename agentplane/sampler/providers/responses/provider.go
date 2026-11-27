package responses

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/jsonwire"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerio"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

const codexReasoningSummary = "auto"

type RequestParams struct {
	Model        string
	Effort       string
	Instructions *string
	Attribution  protocol.SessionAttribution
	Items        []Item
	Tools        protocol.ToolSurface
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
			Parameters: slices.Clone(surface.Parameters),
		}}, nil
	}
	if !json.Valid(surface.WaitParameters) {
		return nil, fmt.Errorf("invalid wait tool parameters: %w", protocol.ErrInvalid)
	}
	strict := false
	return []AdditionalTool{
		{Type: "custom", Name: "exec", Description: surface.Description, Format: &ToolFormat{Type: "grammar", Syntax: "lark", Definition: surface.FreeformGrammar}},
		{Type: "function", Name: "wait", Description: surface.WaitDescription, Strict: &strict, Parameters: slices.Clone(surface.WaitParameters)},
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
	return buildInputFrom(items, config, encoding, map[string]bool{})
}

func buildInputFrom(items []Item, config Config, encoding protocol.ExecEncoding, answered map[string]bool) ([]RequestInput, error) {
	input := make([]RequestInput, 0, len(items))
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

// Sample posts body with the given Authorization value and attribution headers.
func Sample(ctx context.Context, client *http.Client, baseURL, authorization string, encoded []byte, config Config,
	evidence protocol.EvidenceSink, attribution map[string]string, compaction bool, hooks protocol.ProviderHooks,
) (*Response, error) {
	response, err := providerio.Post(ctx, client, providerio.Request{
		URL: strings.TrimRight(baseURL, "/") + "/v1/responses", Body: encoded,
		Headers:  []map[string]string{Headers(config, compaction), attribution, {"Authorization": authorization}},
		Provider: "provider", RequestIDHeader: "x-request-id",
	}, evidence, hooks)
	if err != nil {
		return nil, err
	}
	defer response.Close()
	return readStream(ctx, response.Body, evidence, hooks)
}

func readStream(ctx context.Context, reader io.Reader, evidence protocol.EvidenceSink, hooks protocol.ProviderHooks) (*Response, error) {
	result := &Response{}
	seenItemTransitions := map[string]struct{}{}
	var projector observationProjector
	dispatch := func(data string) error {
		event, err := decodeProviderEvent(data)
		if err != nil {
			return err
		}
		var observations []*eventpb.Event
		var payload streamEvent
		if evidence != nil {
			observations, err = projector.observations(event)
			if err != nil {
				return err
			}
			payload, err = eventPayload(event)
			if err != nil {
				return err
			}
		}
		result.Events = append(result.Events, event)
		semanticProgress := false
		var eventErr error
		switch event.Type {
		case "response.output_text.delta", "response.reasoning_summary_text.delta", "response.reasoning_text.delta",
			EventFunctionCallArgumentsDelta, EventCustomToolCallInputDelta:
			semanticProgress = event.ItemID != "" && event.Delta != nil && *event.Delta != ""
		case EventOutputItemAdded, EventOutputItemDone:
			if event.Item != nil && len(observations) > 0 {
				if identity := providerItemIdentity(*event.Item); identity != "" {
					key := string(event.Type) + "\x00" + identity
					if _, seen := seenItemTransitions[key]; !seen {
						seenItemTransitions[key] = struct{}{}
						semanticProgress = true
					}
				}
			}
			if event.Type == EventOutputItemDone {
				if event.Item == nil {
					eventErr = errors.New("provider output item event has no item object")
					break
				}
				result.Output = append(result.Output, *event.Item)
			}
		case EventCompleted:
			if event.Response == nil {
				eventErr = &protocol.KnownFailureError{
					Message:  "The Responses API completion event had no response object.",
					Evidence: map[string]any{"reason": "completion event has no response object", "events": result.Events},
				}
				break
			}
			if status, present := event.Response.StatusValue(); present && status != "completed" {
				eventErr = &protocol.KnownFailureError{
					Message:  responseFailureMessage(event),
					Evidence: map[string]any{"reason": "unexpected terminal status", "response": event.Response, "events": result.Events},
				}
				break
			}
			// Like Codex, consume output_item.done items. Antigravity through LiteLLM can
			// rewrite reasoning and add items only in terminal output.
			result.Terminal = event.Response
			semanticProgress = true
			eventErr = errProviderComplete
		case EventFailed, EventIncomplete, EventError:
			eventErr = &protocol.KnownFailureError{
				Message:  responseFailureMessage(event),
				Evidence: map[string]any{"terminal": event, "events": result.Events},
			}
		}
		if evidence != nil {
			if err := protocol.RecordProviderEvidence(ctx, evidence, payload, semanticProgress, observations...); err != nil {
				return err
			}
			if semanticProgress && hooks.Progress != nil {
				if err := hooks.Progress(); err != nil {
					return err
				}
			}
		}
		return eventErr
	}
	switch err := providerio.ReadSSE(ctx, reader, dispatch); {
	case errors.Is(err, errProviderComplete):
		return result, nil
	case err != nil:
		return nil, err
	}
	return nil, &protocol.ProviderTransientError{Cause: errors.New("provider stream ended without response.completed")}
}

// Only read diagnostic fields from the terminal event. Other fields can contain the request,
// including its instructions. The journal retains the full event for inspection.
func responseFailureMessage(event Event) string {
	type diagnostic struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	}
	var wire struct {
		diagnostic
		Error    diagnostic `json:"error"`
		Response struct {
			Error             diagnostic `json:"error"`
			ProviderError     diagnostic `json:"provider_error"`
			IncompleteDetails struct {
				Reason string `json:"reason"`
			} `json:"incomplete_details"`
		} `json:"response"`
	}
	if err := json.Unmarshal(event.Raw, &wire); err == nil {
		for _, detail := range []diagnostic{wire.Response.Error, wire.Response.ProviderError, wire.Error, wire.diagnostic} {
			code, message := strings.TrimSpace(detail.Code), strings.TrimSpace(detail.Message)
			switch {
			case message != "" && code != "":
				return fmt.Sprintf("%s (%s)", message, code)
			case message != "":
				return message
			case code != "":
				return fmt.Sprintf("The Responses API reported an error (%s).", code)
			}
		}
		if reason := strings.TrimSpace(wire.Response.IncompleteDetails.Reason); reason != "" {
			return fmt.Sprintf("The Responses API returned an incomplete response (%s).", reason)
		}
	}
	if event.Type == EventIncomplete {
		return "The Responses API returned an incomplete response."
	}
	return "The Responses API reported a failed response."
}

func providerItemIdentity(item Item) string {
	if item.ID == "" && item.CallID == "" {
		return ""
	}
	return string(item.Type) + "\x00" + item.ID + "\x00" + item.CallID
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

func (streamEvent) Kind() eventpb.ExecutionProgress_Kind {
	return eventpb.ExecutionProgress_KIND_PROVIDER
}

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
	value, err := jsonwire.DecodeJSONValue(data)
	if err != nil {
		return streamEvent{}, err
	}
	if _, ok := value.(map[string]any); !ok {
		return streamEvent{}, fmt.Errorf("provider event must be an object: %w", protocol.ErrInvalid)
	}
	return streamEvent{value: value}, nil
}
