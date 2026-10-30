package centralsampler

import (
	"bufio"
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"google.golang.org/protobuf/proto"
)

func anthropicMessageFields(record json.RawMessage) map[string]json.RawMessage {
	var fields map[string]json.RawMessage
	_ = json.Unmarshal(record, &fields)
	return fields
}

func anthropicEndTurn(record json.RawMessage) bool {
	fields := anthropicMessageFields(record)
	return rawString(fields, "role") == "assistant" && rawString(fields, "stop_reason") == "end_turn"
}

func anthropicPreviousMessageID(context *AnthropicContext) string {
	if context == nil {
		return ""
	}
	for i := len(context.Messages) - 1; i >= 0; i-- {
		fields := anthropicMessageFields(context.Messages[i])
		if rawString(fields, "role") == "assistant" && rawString(fields, "stop_reason") != "compaction" {
			return rawString(fields, "id")
		}
	}
	return ""
}

// anthropicSuffixStart keeps whole completed turns, including signed native blocks and any
// associated user tool results.
func anthropicSuffixStart(records []json.RawMessage, keep int) int {
	if keep == 0 {
		return len(records)
	}
	completed := 0
	for i := len(records) - 1; i >= 0; i-- {
		if anthropicEndTurn(records[i]) {
			completed++
			if completed > keep {
				return i + 1
			}
		}
	}
	return 0
}

func isAnthropicEffortControl(record json.RawMessage) bool {
	fields := anthropicMessageFields(record)
	_, hasOutputConfig := fields["output_config"]
	return rawString(fields, "role") == "system" && hasOutputConfig
}

func anthropicParts(parts []ContentPart) ([]any, error) {
	result := make([]any, 0, len(parts))
	for _, part := range parts {
		switch part.Type {
		case "input_text", "output_text", "text":
			result = append(result, map[string]any{"type": "text", "text": part.Text})
		case "input_image":
			var image map[string]json.RawMessage
			if err := json.Unmarshal(part.Raw, &image); err != nil {
				return nil, err
			}
			var address string
			if err := json.Unmarshal(image["image_url"], &address); err != nil {
				return nil, fmt.Errorf("invalid image URL: %w", err)
			}
			if strings.HasPrefix(address, "data:") {
				meta, data, ok := strings.Cut(strings.TrimPrefix(address, "data:"), ",")
				media, base64 := strings.CutSuffix(meta, ";base64")
				if !ok || !base64 || !isOneOf(media, "image/png", "image/jpeg", "image/gif", "image/webp") {
					return nil, errors.New("unsupported Anthropic image data URL")
				}
				result = append(result, map[string]any{"type": "image", "source": map[string]any{"type": "base64", "media_type": media, "data": data}})
			} else {
				return nil, errors.New("Anthropic image input requires a base64 data URL")
			}
		default:
			return nil, fmt.Errorf("unsupported Anthropic input content type %q", part.Type)
		}
	}
	return result, nil
}

type AnthropicRequestParams struct {
	Context      *AnthropicContext
	Model        string
	Effort       string
	Instructions *string
}

func BuildAnthropicRequest(params AnthropicRequestParams, config ModelConfig, previousMessageID string) (map[string]any, error) {
	if err := config.Validate(); err != nil {
		return nil, err
	}
	if config.API != APIStyleAnthropicMessages {
		return nil, errors.New("Anthropic request requires an anthropic_messages route")
	}
	options := config.AnthropicMessages
	description, err := execToolDescription(toolDefinitions(), config)
	if err != nil {
		return nil, err
	}
	tool, err := jsonExecToolDefinition(description)
	if err != nil {
		return nil, err
	}
	var schema map[string]any
	if err := json.Unmarshal(tool.Parameters, &schema); err != nil {
		return nil, err
	}
	delete(schema, "$schema")
	// The wire schema has one object shape; the host enforces code XOR cell_id and numeric bounds.
	// Strict decoding accepts optional properties, but not JSON Schema minimum/maximum keywords.
	if properties, ok := schema["properties"].(map[string]any); ok {
		for _, value := range properties {
			if property, ok := value.(map[string]any); ok {
				delete(property, "minimum")
				delete(property, "maximum")
			}
		}
	}
	context := params.Context
	if context == nil {
		context = &AnthropicContext{}
	}
	messages, err := context.WireMessages()
	if err != nil {
		return nil, err
	}
	system := baseInstructions
	if params.Instructions != nil && *params.Instructions != "" {
		system += "\n\n" + *params.Instructions
	}
	model := params.Model
	if model == "" {
		model = config.ProviderModel
	}
	body := map[string]any{
		"model": model, "max_tokens": options.MaxTokens, "system": system, "messages": messages, "stream": true,
		"tools": []any{map[string]any{"name": "exec", "description": description, "input_schema": schema, "strict": options.StrictToolInput, "eager_input_streaming": options.EagerInputStreaming}},
	}
	if options.Thinking != nil {
		body["thinking"] = options.Thinking
	}
	if params.Effort != "" {
		body["output_config"] = map[string]any{"effort": params.Effort}
	}
	if options.PromptCache != nil {
		body["cache_control"] = map[string]any{"type": "ephemeral", "ttl": options.PromptCache.TTL}
	}
	if options.CacheDiagnostics {
		var previous any
		if previousMessageID != "" {
			previous = previousMessageID
		}
		body["diagnostics"] = map[string]any{"previous_message_id": previous}
	}
	return body, nil
}

func anthropicBetas(config ModelConfig) string {
	options := config.AnthropicMessages
	var betas []string
	// Include capabilities from the first request, so enabling continuation features never changes
	// a cached prefix's headers halfway through a conversation.
	if options.Compaction != nil {
		betas = append(betas, "compact-2026-09-04")
	}
	if options.PerMessageEffort {
		betas = append(betas, "mid-conversation-output-config-2026-07-01")
	}
	if options.Thinking != nil && options.Thinking.BlockBinding != nil {
		betas = append(betas, "thinking-binding-controls-2026-08-01")
	}
	return strings.Join(betas, ",")
}

type AnthropicResponse struct {
	Message           json.RawMessage
	Events            []json.RawMessage
	ContextTokens     *int
	HTTPStatus        int
	ProviderRequestID string
}

func (r AnthropicResponse) Audit() map[string]any {
	return map[string]any{
		"response": r.Message, "events": r.Events, "context_tokens": r.ContextTokens,
		"http_status": r.HTTPStatus, "provider_request_id": r.ProviderRequestID,
	}
}

func SampleAnthropic(ctx context.Context, client *http.Client, connection ConnectionConfig, config ModelConfig, wire []byte, evidence *ResponseEvidence, compaction bool) (*AnthropicResponse, error) {
	credential, err := connection.Auth.ReadCredential()
	if err != nil {
		return nil, &KnownFailureError{Evidence: map[string]any{"reason": "provider credential unavailable", "error": err.Error(), "dispatched": false}}
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(connection.BaseURL, "/")+"/v1/messages", bytes.NewReader(wire))
	if err != nil {
		return nil, err
	}
	request.Header.Set("content-type", "application/json")
	request.Header.Set("anthropic-version", "2023-06-01")
	if betas := anthropicBetas(config); betas != "" {
		request.Header.Set("anthropic-beta", betas)
	}
	if connection.Auth.Type == AuthTypeAnthropicAPIKey {
		request.Header.Set("x-api-key", credential)
	} else {
		request.Header.Set("authorization", "Bearer "+credential)
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	reader := providerCaptureReader{reader: response.Body, ctx: ctx, evidence: evidence}
	metadata := map[string]any{"http_status": response.StatusCode, "provider_request_id": response.Header.Get("request-id"), "content_type": response.Header.Get("content-type")}
	if evidence != nil {
		if err := evidence.Store.ProtocolEvent(ctx, evidence.Owner, evidence.IntentID, "provider_http", "received", metadata); err != nil {
			return nil, err
		}
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		body, err := io.ReadAll(io.LimitReader(reader, maxProviderStreamBytes))
		if err != nil {
			return nil, err
		}
		metadata["body"] = string(body)
		return nil, &KnownFailureError{Evidence: metadata}
	}
	result, err := readAnthropicStream(ctx, io.LimitReader(reader, maxProviderStreamBytes), evidence, compaction)
	if result != nil {
		result.HTTPStatus = response.StatusCode
		result.ProviderRequestID = response.Header.Get("request-id")
	}
	return result, err
}

// AnthropicEvent persists each exact native stream event before the parser acts on it.
func (s *Store) AnthropicEvent(ctx context.Context, owner Owner, intentID, wire string, observations []*eventpb.Event) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwnerHeader(ctx, tx, owner)
		if err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		sources, err := s.recordEvidenceWithSources(ctx, tx, owner, conversation, session, intentID, "provider_anthropic", "received", map[string]any{"wire_event": wire})
		if err != nil {
			return err
		}
		for _, observation := range observations {
			if observation == nil {
				continue
			}
			observation.SourceSequences = sources
			if _, err := appendEvent(ctx, tx, session, observation); err != nil {
				return err
			}
		}
		return nil
	})
}

type anthropicBlock struct {
	fields  map[string]json.RawMessage
	input   strings.Builder
	stopped bool
}

func rawString(fields map[string]json.RawMessage, key string) string {
	var value string
	_ = json.Unmarshal(fields[key], &value)
	return value
}

func anthropicString(fields map[string]json.RawMessage, key string) (string, bool) {
	raw, present := fields[key]
	if !present {
		return "", false
	}
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		return "", false
	}
	return value, true
}

func validateAnthropicOutputBlock(fields map[string]json.RawMessage) error {
	switch rawString(fields, "type") {
	case "text":
		if _, ok := anthropicString(fields, "text"); !ok {
			return errors.New("text block requires a string text field")
		}
		return nil
	case "thinking":
		if _, ok := anthropicString(fields, "thinking"); !ok {
			return errors.New("thinking block requires a string thinking field")
		}
		return nil
	case "redacted_thinking", "compaction":
		return nil
	case "tool_use":
		if rawString(fields, "id") == "" || rawString(fields, "name") == "" {
			return errors.New("tool_use requires an id and name")
		}
		var input map[string]json.RawMessage
		if err := json.Unmarshal(fields["input"], &input); err != nil || input == nil {
			return errors.New("tool_use requires an input object")
		}
		return nil
	default:
		return fmt.Errorf("unsupported Anthropic output block %q", rawString(fields, "type"))
	}
}

func readAnthropicStream(ctx context.Context, reader io.Reader, evidence *ResponseEvidence, compaction bool) (*AnthropicResponse, error) {
	result := &AnthropicResponse{}
	message := map[string]json.RawMessage{}
	var blocks []*anthropicBlock
	started, deltaSeen := false, false
	var usage map[string]json.RawMessage
	fail := func(reason string) error {
		return &KnownFailureError{Evidence: map[string]any{"reason": reason, "events": result.Events}}
	}
	dispatch := func(data string) (eventErr error) {
		var observations []*eventpb.Event
		var event struct {
			Type         string                     `json:"type"`
			Index        *int                       `json:"index"`
			Message      map[string]json.RawMessage `json:"message"`
			ContentBlock map[string]json.RawMessage `json:"content_block"`
			Delta        map[string]json.RawMessage `json:"delta"`
			Usage        map[string]json.RawMessage `json:"usage"`
		}
		// Even malformed/unsupported events are durable before the parser returns to the runtime.
		defer func() {
			if evidence != nil {
				if err := evidence.Store.AnthropicEvent(ctx, evidence.Owner, evidence.IntentID, data, observations); err != nil {
					eventErr = err
				}
			}
		}()
		result.Events = append(result.Events, json.RawMessage(data))
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			return fmt.Errorf("invalid Anthropic stream event: %w", err)
		}
		if event.Type == "error" {
			return fail("Anthropic stream error")
		}
		if event.Type == "ping" {
			return nil
		}
		switch event.Type {
		case "message_start":
			if started || event.Message == nil {
				return fail("invalid message_start ordering")
			}
			started = true
			message = event.Message
			if rawString(message, "id") == "" || rawString(message, "role") != "assistant" {
				return fail("invalid Anthropic message identity")
			}
			if err := json.Unmarshal(message["usage"], &usage); err != nil || usage == nil {
				return fail("invalid Anthropic usage")
			}
			var initial []json.RawMessage
			if err := json.Unmarshal(message["content"], &initial); err != nil || initial == nil || len(initial) != 0 {
				return fail("message_start must have empty content")
			}
		case "content_block_start":
			if !started || deltaSeen || event.Index == nil || *event.Index != len(blocks) || event.ContentBlock == nil {
				return fail("invalid content block ordering")
			}
			if len(blocks) > 0 && !blocks[len(blocks)-1].stopped {
				return fail("overlapping content blocks")
			}
			block := &anthropicBlock{fields: event.ContentBlock}
			if err := validateAnthropicOutputBlock(block.fields); err != nil {
				return fail(err.Error())
			}
			blocks = append(blocks, block)
			if observation := anthropicItemStarted(message, *event.Index, block.fields); observation != nil {
				observations = append(observations, observation)
			}
		case "content_block_delta":
			if event.Index == nil || *event.Index < 0 || *event.Index >= len(blocks) || blocks[*event.Index].stopped || deltaSeen {
				return fail("invalid content delta ordering")
			}
			block := blocks[*event.Index]
			kind := rawString(event.Delta, "type")
			field := ""
			delta := ""
			switch kind {
			case "text_delta":
				field = "text"
				var ok bool
				delta, ok = anthropicString(event.Delta, "text")
				if !ok {
					return fail("text_delta requires a string text field")
				}
			case "thinking_delta":
				field = "thinking"
				var ok bool
				delta, ok = anthropicString(event.Delta, "thinking")
				if !ok {
					return fail("thinking_delta requires a string thinking field")
				}
			case "signature_delta":
				field = "signature"
				var ok bool
				delta, ok = anthropicString(event.Delta, "signature")
				if !ok {
					return fail("signature_delta requires a string signature field")
				}
			case "input_json_delta":
				partial, ok := anthropicString(event.Delta, "partial_json")
				if !ok {
					return fail("input_json_delta requires a string partial_json field")
				}
				block.input.WriteString(partial)
			default:
				return fail("unsupported Anthropic delta " + kind)
			}
			expected := map[string]string{"text_delta": "text", "thinking_delta": "thinking", "signature_delta": "thinking", "input_json_delta": "tool_use"}
			if rawString(block.fields, "type") != expected[kind] {
				return fail("delta does not match content block")
			}
			if field != "" {
				encoded, err := json.Marshal(rawString(block.fields, field) + delta)
				if err != nil {
					return err
				}
				block.fields[field] = encoded
			}
			if observation := anthropicBlockDelta(message, *event.Index, block.fields, kind, delta); observation != nil {
				observations = append(observations, observation)
			}
		case "content_block_stop":
			if event.Index == nil || *event.Index < 0 || *event.Index >= len(blocks) || blocks[*event.Index].stopped || deltaSeen {
				return fail("invalid content stop ordering")
			}
			block := blocks[*event.Index]
			block.stopped = true
			if rawString(block.fields, "type") == "tool_use" && block.input.Len() > 0 {
				block.fields["input"] = json.RawMessage(block.input.String())
			}
			if rawString(block.fields, "type") == "tool_use" {
				var input map[string]json.RawMessage
				if err := json.Unmarshal(block.fields["input"], &input); err != nil || input == nil {
					return fail("tool input is not a complete JSON object")
				}
				if observation := anthropicToolArguments(message, *event.Index, block.fields); observation != nil {
					observations = append(observations, observation)
				}
			} else if observation := anthropicBlockCompleted(message, *event.Index, block.fields); observation != nil {
				observations = append(observations, observation)
			}
		case "message_delta":
			if !started || deltaSeen {
				return fail("invalid message_delta ordering")
			}
			for _, block := range blocks {
				if !block.stopped {
					return fail("message_delta before content stop")
				}
			}
			deltaSeen = true
			for key, value := range event.Delta {
				message[key] = value
			}
			for key, value := range event.Usage {
				usage[key] = value
			}
		case "message_stop":
			if !started || !deltaSeen {
				return fail("message_stop without message delta")
			}
			stop := rawString(message, "stop_reason")
			tools, compactions := 0, 0
			content := make([]json.RawMessage, 0, len(blocks))
			seen := map[string]bool{}
			for _, block := range blocks {
				if !block.stopped {
					return fail("message_stop before content stop")
				}
				switch rawString(block.fields, "type") {
				case "tool_use":
					tools++
					id := rawString(block.fields, "id")
					if id == "" || seen[id] || rawString(block.fields, "name") == "" {
						return fail("invalid or duplicate tool identity")
					}
					seen[id] = true
				case "compaction":
					compactions++
					if rawString(block.fields, "signature") == "" || rawString(block.fields, "content") == "" {
						return fail("empty or unsigned compaction block")
					}
				case "thinking":
					if rawString(block.fields, "signature") == "" {
						return fail("unsigned thinking block")
					}
				}
				encoded, err := json.Marshal(block.fields)
				if err != nil {
					return err
				}
				content = append(content, encoded)
			}
			if compaction {
				if stop != "compaction" || compactions != 1 || len(blocks) != 1 {
					return fail("compaction must return one signed compaction block")
				}
			} else if !((stop == "end_turn" && tools == 0 && compactions == 0) || (stop == "tool_use" && tools > 0 && compactions == 0)) {
				return fail("unsupported Anthropic stop reason or inconsistent output: " + stop)
			}
			encoded, err := json.Marshal(content)
			if err != nil {
				return err
			}
			message["content"] = encoded
			encoded, err = json.Marshal(usage)
			if err != nil {
				return err
			}
			message["usage"] = encoded
			result.Message, err = json.Marshal(message)
			if err != nil {
				return err
			}
			total := 0
			for _, key := range []string{"input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"} {
				var value int
				if err := json.Unmarshal(usage[key], &value); err == nil {
					total += value
				}
			}
			result.ContextTokens = &total
			return errProviderComplete
		default:
			return fail("unsupported Anthropic stream event " + event.Type)
		}

		return nil
	}
	lineReader := bufio.NewReader(reader)
	var lines []string
	for {
		line, readErr := lineReader.ReadString('\n')
		if len(line) > 0 {
			line = strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
			if strings.HasPrefix(line, "data:") {
				lines = append(lines, strings.TrimLeft(line[5:], " \t"))
			} else if line == "" && len(lines) > 0 {
				eventErr := dispatch(strings.Join(lines, "\n"))
				lines = nil
				if errors.Is(eventErr, errProviderComplete) {
					return result, nil
				}
				if eventErr != nil {
					return nil, eventErr
				}
			}
		}
		if readErr != nil {
			if !errors.Is(readErr, io.EOF) {
				return nil, readErr
			}
			return nil, &UnknownEffectError{Reason: "Anthropic stream ended before message_stop; sampling outcome unknown"}
		}
	}
}

func (r *Runtime) compactAnthropic(ctx context.Context, owner Owner, model string, conversation *Conversation, metadata SessionAttribution, state *ExecutionState) error {
	config := r.settings.Models[model]
	options := config.AnthropicMessages
	if options.Compaction == nil {
		return &KnownFailureError{Evidence: map[string]any{"reason": "Anthropic compaction is not enabled for this route"}}
	}
	original, ok := conversation.Context().(*AnthropicContext)
	if !ok {
		return fmt.Errorf("Anthropic compaction received %T context", conversation.Context())
	}
	records, err := original.Records()
	if err != nil {
		return err
	}
	cut := anthropicSuffixStart(records, options.Compaction.KeepRecentTurns)
	// A kept system control would merge with the summarized prefix, breaking preserved thinking.
	// Keep its following turn by moving the boundary back to the previous completed turn.
	for cut > 0 && cut < len(records) && isAnthropicEffortControl(records[cut]) {
		cut = anthropicSuffixStart(records[:cut], 1)
	}
	if cut == 0 {
		return &KnownFailureError{Evidence: map[string]any{"reason": "no settled prefix remains after keeping recent turns"}}
	}
	prefix := &AnthropicContext{Messages: records[:cut]}
	if err := prefix.CheckSettled(); err != nil {
		return err
	}
	var session *Session
	if err := r.store.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := lockOwnerHeader(ctx, tx, owner); err != nil {
			return err
		}
		var err error
		session, err = sessionForConversation(ctx, tx, conversation.ID)
		return err
	}); err != nil {
		return err
	}
	launch := &samplerpb.OpenSessionRequest{}
	if err := proto.Unmarshal(session.Configuration, launch); err != nil {
		return err
	}
	effort := session.Effort
	if options.PerMessageEffort {
		effort = launch.GetSpec().GetReasoningEffort()
	}
	body, err := BuildAnthropicRequest(AnthropicRequestParams{
		Context: prefix, Model: config.ProviderModel, Effort: effort,
		Instructions: conversation.Instructions,
	}, config, anthropicPreviousMessageID(original))
	if err != nil {
		return err
	}
	body["compaction"] = map[string]any{"type": "summarize"}
	body["max_tokens"] = options.Compaction.MaxTokens
	wire, err := json.Marshal(body)
	if err != nil {
		return err
	}
	connection, err := r.settings.Connection(model)
	if err != nil {
		return err
	}
	intentID, err := r.store.BeginCompaction(ctx, owner, providerRequestAudit("compaction", connection, config, body, wire), original)
	if err != nil {
		return err
	}
	response, err := SampleAnthropic(ctx, &http.Client{}, connection, config, wire, &ResponseEvidence{Store: r.store, Owner: owner, IntentID: intentID}, true)
	if err != nil {
		var known *KnownFailureError
		if errors.As(err, &known) {
			if rejectErr := r.store.Reject(ctx, owner, intentID, known); rejectErr != nil {
				return rejectErr
			}
		}
		return err
	}
	replacement := &AnthropicContext{}
	if err := json.Unmarshal(mustJSON(original), replacement); err != nil {
		return err
	}
	replacement.Messages = []json.RawMessage{cloneJSON(response.Message)}
	replacement.Messages = append(replacement.Messages, records[cut:]...)
	if options.PerMessageEffort {
		effort := session.Effort
		replacement.PendingEffort = &effort
	}
	originalForSize := &AnthropicContext{}
	if err := json.Unmarshal(mustJSON(original), originalForSize); err != nil {
		return err
	}
	replacementForSize := &AnthropicContext{}
	if err := json.Unmarshal(mustJSON(replacement), replacementForSize); err != nil {
		return err
	}
	originalMessages, err := originalForSize.WireMessages()
	if err != nil {
		return err
	}
	replacementMessages, err := replacementForSize.WireMessages()
	if err != nil {
		return err
	}
	if len(mustJSON(replacementMessages)) >= len(mustJSON(originalMessages)) {
		failure := &KnownFailureError{Evidence: map[string]any{"reason": "compaction did not reduce serialized model context", "response": response.Audit()}}
		if err := r.store.Reject(ctx, owner, intentID, failure); err != nil {
			return err
		}
		return failure
	}
	audit := response.Audit()
	audit["replacement"] = replacement
	return r.store.FinishCompaction(ctx, owner, intentID, audit, original, replacement)
}
