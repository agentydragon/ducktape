package responses

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/storage"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

const (
	ContentOpenAIItem    storage.ContentKind = "openai_item"
	ContentOpenAIRequest storage.ContentKind = "openai_request"
	ContentOpenAITool    storage.ContentKind = "openai_tool"
)

type storedRequestProfile struct {
	Envelope         map[string]json.RawMessage `json:"envelope,omitempty"`
	Model            string                     `json:"model"`
	Stream           bool                       `json:"stream"`
	Store            bool                       `json:"store"`
	Include          []string                   `json:"include,omitempty"`
	PromptCacheKey   string                     `json:"prompt_cache_key"`
	ClientMetadata   *ClientMetadata            `json:"client_metadata,omitempty"`
	Reasoning        *ReasoningConfig           `json:"reasoning,omitempty"`
	ParallelToolCall bool                       `json:"parallel_tool_calls"`
	Instructions     storage.ContentID          `json:"instructions_ref"`
	Tools            []storage.ContentID        `json:"tool_refs,omitempty"`
}

type requestProfileFields struct {
	Model            string           `json:"model"`
	Instructions     string           `json:"instructions"`
	Stream           bool             `json:"stream"`
	Store            bool             `json:"store"`
	Include          []string         `json:"include,omitempty"`
	PromptCacheKey   string           `json:"prompt_cache_key"`
	ClientMetadata   *ClientMetadata  `json:"client_metadata,omitempty"`
	Reasoning        *ReasoningConfig `json:"reasoning,omitempty"`
	ParallelToolCall bool             `json:"parallel_tool_calls"`
}

// StoreRecord stores one complete native Responses item as the deduplication unit.
// Its message parts, tool arguments and opaque continuation remain inline.
func StoreRecord(writer storage.ContentWriter, record json.RawMessage) (storage.ContentID, error) {
	if writer == nil {
		return "", errors.New("nil content writer")
	}
	if err := validateStoredItem(record); err != nil {
		return "", err
	}
	return writer.PutContent(ContentOpenAIItem, record)
}

// RestoreRecord loads a complete native item without rebuilding its internal fields.
func RestoreRecord(reader storage.ContentReader, id storage.ContentID) (json.RawMessage, error) {
	payload, err := getPayload(reader, id, ContentOpenAIItem)
	if err != nil {
		return nil, err
	}
	if err := validateStoredItem(payload); err != nil {
		return nil, err
	}
	return payload, nil
}

func validateStoredItem(record json.RawMessage) error {
	var item struct {
		Type ItemType `json:"type"`
	}
	if err := json.Unmarshal(record, &item); err != nil {
		return fmt.Errorf("decode Responses item: %w", err)
	}
	if item.Type == "" {
		return fmt.Errorf("Responses item has no type: %w", protocol.ErrInvalid)
	}
	return nil
}

// StoreRequest splits a native request into an immutable profile and ordered
// item references. Instructions and tools are shared independently of per-call
// attribution and other profile fields.
func StoreRequest(writer storage.ContentWriter, request Request) (profile storage.ContentID, records []storage.ContentID, err error) {
	if writer == nil {
		return "", nil, errors.New("nil content writer")
	}
	records = make([]storage.ContentID, 0, len(request.Input))
	for index, input := range request.Input {
		raw, marshalErr := json.Marshal(input)
		if marshalErr != nil {
			return "", nil, fmt.Errorf("encode request input %d: %w", index, marshalErr)
		}
		id, storeErr := StoreRecord(writer, raw)
		if storeErr != nil {
			return "", nil, fmt.Errorf("store request input %d: %w", index, storeErr)
		}
		records = append(records, id)
	}
	toolRecords := make([]json.RawMessage, 0, len(request.Tools))
	for index, tool := range request.Tools {
		raw, marshalErr := json.Marshal(tool)
		if marshalErr != nil {
			return "", nil, fmt.Errorf("encode request tool %d: %w", index, marshalErr)
		}
		toolRecords = append(toolRecords, raw)
	}
	profile, err = storeRequestProfile(writer, request, toolRecords, nil)
	if err != nil {
		return "", nil, err
	}
	return profile, records, nil
}

// StoreRequestJSON is a migration-only path for pre-profile request JSON. It
// splits raw input items before decoding the remaining request envelope so
// provider-owned item fields and number tokens are retained.
func StoreRequestJSON(writer storage.ContentWriter, raw json.RawMessage) (profile storage.ContentID, records []storage.ContentID, err error) {
	if writer == nil {
		return "", nil, errors.New("nil content writer")
	}
	fields, err := rawObject(raw)
	if err != nil {
		return "", nil, err
	}
	inputRaw, hasInput := fields["input"]
	delete(fields, "input")
	if hasInput {
		parts, isArray := jsonArray(inputRaw)
		if !isArray {
			return "", nil, fmt.Errorf("historical Responses request input must be an array: %w", protocol.ErrInvalid)
		}
		records = make([]storage.ContentID, 0, len(parts))
		for index, item := range parts {
			id, storeErr := StoreRecord(writer, item)
			if storeErr != nil {
				return "", nil, fmt.Errorf("store historical request input %d: %w", index, storeErr)
			}
			records = append(records, id)
		}
	}
	toolRecords := []json.RawMessage(nil)
	profileEnvelope := make(map[string]json.RawMessage)
	if toolsRaw, present := fields["tools"]; present {
		if tools, isArray := jsonArray(toolsRaw); isArray && len(tools) != 0 {
			toolRecords = tools
		} else {
			// Keep null, empty, or otherwise non-array historical fields verbatim.
			profileEnvelope["tools"] = protocol.CloneJSON(toolsRaw)
		}
		delete(fields, "tools")
	}
	for name, value := range fields {
		if !isRequestProfileField(name) {
			profileEnvelope[name] = protocol.CloneJSON(value)
		}
	}
	requestJSON, err := json.Marshal(fields)
	if err != nil {
		return "", nil, err
	}
	var request Request
	if err := json.Unmarshal(requestJSON, &request); err != nil {
		return "", nil, fmt.Errorf("decode historical Responses request profile: %w", err)
	}
	if len(profileEnvelope) == 0 {
		profileEnvelope = nil
	}
	profile, err = storeRequestProfile(writer, request, toolRecords, profileEnvelope)
	if err != nil {
		return "", nil, err
	}
	return profile, records, nil
}

// ContentReferences returns a request profile's shared instructions and tools.
// Native items and tool definitions are leaves; their provider fields are not references.
func ContentReferences(kind storage.ContentKind, payload json.RawMessage) ([]storage.ContentID, error) {
	var refs []storage.ContentID
	add := func(id storage.ContentID) error {
		if id == "" {
			return fmt.Errorf("stored Responses %s has an empty content reference: %w", kind, protocol.ErrInvalid)
		}
		refs = append(refs, id)
		return nil
	}

	switch kind {
	case ContentOpenAIRequest:
		var stored storedRequestProfile
		if err := json.Unmarshal(payload, &stored); err != nil {
			return nil, fmt.Errorf("decode stored Responses request references: %w", err)
		}
		if err := add(stored.Instructions); err != nil {
			return nil, err
		}
		for _, id := range stored.Tools {
			if err := add(id); err != nil {
				return nil, err
			}
		}
	}
	return refs, nil
}

func storeRequestProfile(
	writer storage.ContentWriter,
	request Request,
	toolRecords []json.RawMessage,
	envelope map[string]json.RawMessage,
) (storage.ContentID, error) {
	instructionID, err := putString(writer, storage.ContentText, request.Instructions)
	if err != nil {
		return "", fmt.Errorf("store request instructions: %w", err)
	}
	toolIDs := make([]storage.ContentID, 0, len(toolRecords))
	for index, tool := range toolRecords {
		if _, err := rawObject(tool); err != nil {
			return "", fmt.Errorf("request tool %d: %w", index, err)
		}
		id, err := writer.PutContent(ContentOpenAITool, tool)
		if err != nil {
			return "", fmt.Errorf("store request tool %d: %w", index, err)
		}
		toolIDs = append(toolIDs, id)
	}
	stored := storedRequestProfile{
		Envelope: envelope, Model: request.Model, Stream: request.Stream, Store: request.Store,
		Include: request.Include, PromptCacheKey: request.PromptCacheKey, ClientMetadata: request.ClientMetadata,
		Reasoning: request.Reasoning, ParallelToolCall: request.ParallelToolCalls,
		Instructions: instructionID, Tools: toolIDs,
	}
	payload, err := json.Marshal(stored)
	if err != nil {
		return "", err
	}
	return writer.PutContent(ContentOpenAIRequest, payload)
}

// RestoreRequest restores a request profile and inserts the already-restored
// native item JSON records in their original order.
func RestoreRequest(reader storage.ContentReader, profile storage.ContentID, records []json.RawMessage) ([]byte, error) {
	payload, err := getPayload(reader, profile, ContentOpenAIRequest)
	if err != nil {
		return nil, err
	}
	var stored storedRequestProfile
	if err := json.Unmarshal(payload, &stored); err != nil {
		return nil, fmt.Errorf("decode stored Responses request profile: %w", err)
	}
	instructions, err := restoreString(reader, stored.Instructions, storage.ContentText)
	if err != nil {
		return nil, fmt.Errorf("restore request instructions: %w", err)
	}
	tools := make([]json.RawMessage, 0, len(stored.Tools))
	for index, id := range stored.Tools {
		toolPayload, err := getPayload(reader, id, ContentOpenAITool)
		if err != nil {
			return nil, fmt.Errorf("restore request tool %d: %w", index, err)
		}
		if _, err := rawObject(toolPayload); err != nil {
			return nil, fmt.Errorf("decode stored request tool %d: %w", index, err)
		}
		tools = append(tools, protocol.CloneJSON(toolPayload))
	}
	input := make([]json.RawMessage, 0, len(records))
	for index, record := range records {
		if _, err := rawObject(record); err != nil {
			return nil, fmt.Errorf("request input %d: %w", index, err)
		}
		input = append(input, protocol.CloneJSON(record))
	}
	request := Request{
		Model: stored.Model, Instructions: instructions, Stream: stored.Stream, Store: stored.Store,
		Include: stored.Include, PromptCacheKey: stored.PromptCacheKey, ClientMetadata: stored.ClientMetadata,
		Reasoning: stored.Reasoning, ParallelToolCalls: stored.ParallelToolCall,
	}
	if len(stored.Envelope) == 0 {
		return json.Marshal(requestWithTools{Request: request, Input: input, Tools: tools})
	}

	// Historical requests may have provider-owned top-level fields. Marshal only
	// the small native profile, then add raw extension fields and input records.
	profileFields := requestProfileFields{
		Model: request.Model, Instructions: request.Instructions, Stream: request.Stream, Store: request.Store,
		Include: request.Include, PromptCacheKey: request.PromptCacheKey, ClientMetadata: request.ClientMetadata,
		Reasoning: request.Reasoning, ParallelToolCall: request.ParallelToolCalls,
	}
	profileJSON, err := json.Marshal(profileFields)
	if err != nil {
		return nil, err
	}
	envelope, err := rawObject(profileJSON)
	if err != nil {
		return nil, err
	}
	for name, value := range stored.Envelope {
		if err := setRawField(envelope, name, value); err != nil {
			return nil, err
		}
	}
	if len(tools) != 0 {
		toolJSON, err := json.Marshal(tools)
		if err != nil {
			return nil, err
		}
		if err := setRawField(envelope, "tools", toolJSON); err != nil {
			return nil, err
		}
	}
	inputJSON, err := json.Marshal(records)
	if err != nil {
		return nil, err
	}
	if err := setRawField(envelope, "input", inputJSON); err != nil {
		return nil, err
	}
	return json.Marshal(envelope)
}

type requestWithTools struct {
	Request
	Input []json.RawMessage `json:"input"`
	Tools []json.RawMessage `json:"tools,omitempty"`
}

func putString(writer storage.ContentWriter, kind storage.ContentKind, value string) (storage.ContentID, error) {
	payload, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	return writer.PutContent(kind, payload)
}

func restoreString(reader storage.ContentReader, id storage.ContentID, kind storage.ContentKind) (string, error) {
	payload, err := getPayload(reader, id, kind)
	if err != nil {
		return "", err
	}
	var value string
	if err := json.Unmarshal(payload, &value); err != nil {
		return "", fmt.Errorf("decode stored %s string: %w", kind, err)
	}
	return value, nil
}

func getPayload(reader storage.ContentReader, id storage.ContentID, kind storage.ContentKind) (json.RawMessage, error) {
	if reader == nil {
		return nil, errors.New("nil content reader")
	}
	content, err := reader.GetContent(id)
	if err != nil {
		return nil, err
	}
	if content.ID != id || content.Kind != kind {
		return nil, fmt.Errorf("stored content %s has unexpected kind or identity: %w", id, protocol.ErrInvalid)
	}
	return protocol.CloneJSON(content.Payload), nil
}

func rawObject(raw json.RawMessage) (map[string]json.RawMessage, error) {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return nil, fmt.Errorf("expected JSON object: %w", protocol.ErrInvalid)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(trimmed, &fields); err != nil {
		return nil, fmt.Errorf("decode JSON object: %w", err)
	}
	if fields == nil {
		return nil, fmt.Errorf("expected JSON object: %w", protocol.ErrInvalid)
	}
	return fields, nil
}

func jsonArray(raw json.RawMessage) ([]json.RawMessage, bool) {
	if !bytes.HasPrefix(bytes.TrimSpace(raw), []byte("[")) {
		return nil, false
	}
	var values []json.RawMessage
	if err := json.Unmarshal(raw, &values); err != nil {
		return nil, false
	}
	return values, true
}

func setRawField(fields map[string]json.RawMessage, name string, value json.RawMessage) error {
	if _, exists := fields[name]; exists {
		return fmt.Errorf("stored Responses envelope duplicates field %q: %w", name, protocol.ErrInvalid)
	}
	if !json.Valid(value) {
		return fmt.Errorf("stored Responses envelope has invalid JSON for %q: %w", name, protocol.ErrInvalid)
	}
	fields[name] = protocol.CloneJSON(value)
	return nil
}

func isRequestProfileField(name string) bool {
	switch name {
	case "model", "instructions", "stream", "store", "include", "prompt_cache_key", "client_metadata", "reasoning", "tools", "parallel_tool_calls":
		return true
	default:
		return false
	}
}

// ProjectRecords renders only newly appended records. Earlier outputs establish
// whether an additional output must become an asynchronous notification.
func (state *Context) ProjectRecords(start int, config Config, encoding protocol.ExecEncoding) ([]json.RawMessage, error) {
	if start < 0 || start > len(state.items) {
		return nil, protocol.ErrInvalid
	}
	answered := map[string]bool{}
	for _, item := range state.items[:start] {
		if item.IsToolOutput() {
			answered[item.CallID] = true
		}
	}
	input, err := buildInputFrom(state.items[start:], config, encoding, answered)
	if err != nil {
		return nil, err
	}
	records := make([]json.RawMessage, 0, len(input))
	for _, item := range input {
		raw, err := json.Marshal(item)
		if err != nil {
			return nil, err
		}
		records = append(records, raw)
	}
	return records, nil
}

// FreezeRequest retains the authored definitions associated with native continuation.
func FreezeRequest(reader storage.ContentReader, profile storage.ContentID, body Request) (Request, error) {
	raw, err := RestoreRequest(reader, profile, nil)
	if err != nil {
		return Request{}, err
	}
	var previous Request
	if err := json.Unmarshal(raw, &previous); err != nil {
		return Request{}, err
	}
	body.Instructions = previous.Instructions
	body.Tools = previous.Tools
	return body, nil
}

// FreezeToolDeclaration preserves Lite's authored tool catalog as an input prefix.
func FreezeToolDeclaration(reader storage.ContentReader, first storage.ContentID, body Request) (Request, error) {
	if len(body.Input) == 0 {
		return body, nil
	}
	item, ok := body.Input[0].(Item)
	if !ok || item.Type != ItemAdditionalTools {
		return body, nil
	}
	raw, err := RestoreRecord(reader, first)
	if err != nil {
		return Request{}, err
	}
	var frozen Item
	if err := json.Unmarshal(raw, &frozen); err != nil {
		return Request{}, err
	}
	if frozen.Type == ItemAdditionalTools {
		body.Input[0] = frozen
	}
	return body, nil
}
