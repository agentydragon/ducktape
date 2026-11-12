package storage

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
)

// storedItem keeps provider-owned fields as RawMessages while splitting repeated
// text and known content arrays into separately addressable content rows.
type storedItem struct {
	Type             responses.ItemType         `json:"type"`
	Envelope         map[string]json.RawMessage `json:"envelope,omitempty"`
	Content          *[]ContentID               `json:"content_refs,omitempty"`
	Summary          *[]ContentID               `json:"summary_refs,omitempty"`
	Output           *[]ContentID               `json:"output_refs,omitempty"`
	Input            *ContentID                 `json:"input_text_ref,omitempty"`
	ContentText      *ContentID                 `json:"content_text_ref,omitempty"`
	Arguments        *ContentID                 `json:"arguments_text_ref,omitempty"`
	OutputText       *ContentID                 `json:"output_text_ref,omitempty"`
	EncryptedContent *ContentID                 `json:"encrypted_content_ref,omitempty"`
}

type storedPart struct {
	Raw       json.RawMessage            `json:"raw,omitempty"`
	Type      *string                    `json:"type,omitempty"`
	Envelope  map[string]json.RawMessage `json:"envelope,omitempty"`
	Text      *ContentID                 `json:"text_ref,omitempty"`
	ImageData *storedImageData           `json:"image_data,omitempty"`
}

type storedImageData struct {
	Field  string    `json:"field"`
	Prefix string    `json:"prefix"`
	Ref    ContentID `json:"ref"`
}

type storedRequestProfile struct {
	Envelope         map[string]json.RawMessage `json:"envelope,omitempty"`
	Model            string                     `json:"model"`
	Stream           bool                       `json:"stream"`
	Store            bool                       `json:"store"`
	Include          []string                   `json:"include,omitempty"`
	PromptCacheKey   string                     `json:"prompt_cache_key"`
	ClientMetadata   *responses.ClientMetadata  `json:"client_metadata,omitempty"`
	Reasoning        *responses.ReasoningConfig `json:"reasoning,omitempty"`
	ParallelToolCall bool                       `json:"parallel_tool_calls"`
	Instructions     ContentID                  `json:"instructions_ref"`
	Tools            []ContentID                `json:"tool_refs,omitempty"`
}

type requestProfileFields struct {
	Model            string                     `json:"model"`
	Instructions     string                     `json:"instructions"`
	Stream           bool                       `json:"stream"`
	Store            bool                       `json:"store"`
	Include          []string                   `json:"include,omitempty"`
	PromptCacheKey   string                     `json:"prompt_cache_key"`
	ClientMetadata   *responses.ClientMetadata  `json:"client_metadata,omitempty"`
	Reasoning        *responses.ReasoningConfig `json:"reasoning,omitempty"`
	ParallelToolCall bool                       `json:"parallel_tool_calls"`
}

// StoreResponsesRecord stores one native Responses item, removing every extracted field
// from its provider envelope before writing the item row.
func StoreResponsesRecord(writer ContentWriter, record json.RawMessage) (ContentID, error) {
	if writer == nil {
		return "", errors.New("nil content writer")
	}
	fields, err := rawObject(record)
	if err != nil {
		return "", err
	}
	typeRaw, ok := fields["type"]
	if !ok {
		return "", fmt.Errorf("Responses item has no type: %w", protocol.ErrInvalid)
	}
	var itemType responses.ItemType
	if err := json.Unmarshal(typeRaw, &itemType); err != nil || itemType == "" {
		return "", fmt.Errorf("Responses item has invalid type: %w", protocol.ErrInvalid)
	}
	delete(fields, "type")
	stored := storedItem{Type: itemType}

	for _, field := range []struct {
		name string
		dst  **ContentID
	}{{"input", &stored.Input}, {"arguments", &stored.Arguments}, {"content", &stored.ContentText}} {
		if raw, present := fields[field.name]; present {
			if value, isString := jsonString(raw); isString {
				id, err := putString(writer, ContentText, value)
				if err != nil {
					return "", err
				}
				*field.dst = &id
				delete(fields, field.name)
			}
		}
	}
	if raw, present := fields["output"]; present {
		if value, isString := jsonString(raw); isString {
			id, err := putString(writer, ContentText, value)
			if err != nil {
				return "", err
			}
			stored.OutputText = &id
			delete(fields, "output")
		} else if parts, isArray := jsonArray(raw); isArray {
			refs, err := storeParts(writer, parts)
			if err != nil {
				return "", fmt.Errorf("store output parts: %w", err)
			}
			stored.Output = &refs
			delete(fields, "output")
		}
	}
	for _, field := range []struct {
		name string
		dst  **[]ContentID
	}{{"content", &stored.Content}, {"summary", &stored.Summary}} {
		if raw, present := fields[field.name]; present {
			if parts, isArray := jsonArray(raw); isArray {
				refs, err := storeParts(writer, parts)
				if err != nil {
					return "", fmt.Errorf("store %s parts: %w", field.name, err)
				}
				*field.dst = &refs
				delete(fields, field.name)
			}
		}
	}
	if raw, present := fields["encrypted_content"]; present {
		if value, isString := jsonString(raw); isString {
			id, err := putString(writer, ContentOpaque, value)
			if err != nil {
				return "", err
			}
			stored.EncryptedContent = &id
			delete(fields, "encrypted_content")
		}
	}
	if len(fields) != 0 {
		stored.Envelope = fields
	}
	payload, err := json.Marshal(stored)
	if err != nil {
		return "", err
	}
	return writer.PutContent(ContentOpenAIItem, payload)
}

// RestoreResponsesRecord reconstructs one native Responses item and verifies every
// referenced row before its bytes are exposed to the provider adapter.
func RestoreResponsesRecord(reader ContentReader, id ContentID) (json.RawMessage, error) {
	payload, err := getPayload(reader, id, ContentOpenAIItem)
	if err != nil {
		return nil, err
	}
	return restoreRecordPayload(reader, id, payload)
}

func restoreRecordPayload(reader ContentReader, id ContentID, payload json.RawMessage) (json.RawMessage, error) {
	var stored storedItem
	if err := json.Unmarshal(payload, &stored); err != nil {
		return nil, fmt.Errorf("decode stored Responses item %s: %w", id, err)
	}
	if stored.Type == "" {
		return nil, fmt.Errorf("stored Responses item has no type: %w", protocol.ErrInvalid)
	}
	fields := cloneEnvelope(stored.Envelope)
	if err := setField(fields, "type", stored.Type); err != nil {
		return nil, err
	}
	for _, value := range []struct {
		name string
		ref  *ContentID
	}{{"input", stored.Input}, {"arguments", stored.Arguments}, {"output", stored.OutputText}, {"content", stored.ContentText}} {
		if value.ref != nil {
			text, err := restoreString(reader, *value.ref, ContentText)
			if err != nil {
				return nil, fmt.Errorf("restore %s: %w", value.name, err)
			}
			if err := setField(fields, value.name, text); err != nil {
				return nil, err
			}
		}
	}
	if stored.EncryptedContent != nil {
		text, err := restoreString(reader, *stored.EncryptedContent, ContentOpaque)
		if err != nil {
			return nil, fmt.Errorf("restore encrypted_content: %w", err)
		}
		if err := setField(fields, "encrypted_content", text); err != nil {
			return nil, err
		}
	}
	for _, value := range []struct {
		name string
		refs *[]ContentID
	}{{"content", stored.Content}, {"summary", stored.Summary}, {"output", stored.Output}} {
		if value.refs != nil {
			parts := make([]json.RawMessage, 0, len(*value.refs))
			for _, partID := range *value.refs {
				part, err := restorePart(reader, partID)
				if err != nil {
					return nil, fmt.Errorf("restore %s part: %w", value.name, err)
				}
				parts = append(parts, part)
			}
			if err := setField(fields, value.name, parts); err != nil {
				return nil, err
			}
		}
	}
	return json.Marshal(fields)
}

// StoreResponsesRequest splits a native request into an immutable profile and ordered
// item references. Instructions and tools are shared independently of per-call
// attribution and other profile fields.
func StoreResponsesRequest(writer ContentWriter, request responses.Request) (profile ContentID, records []ContentID, err error) {
	if writer == nil {
		return "", nil, errors.New("nil content writer")
	}
	records = make([]ContentID, 0, len(request.Input))
	for index, input := range request.Input {
		raw, marshalErr := json.Marshal(input)
		if marshalErr != nil {
			return "", nil, fmt.Errorf("encode request input %d: %w", index, marshalErr)
		}
		id, storeErr := StoreResponsesRecord(writer, raw)
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

// StoreResponsesRequestJSON is a migration-only path for pre-profile request JSON. It
// splits raw input items before decoding the remaining request envelope so
// provider-owned item fields and number tokens are retained.
func StoreResponsesRequestJSON(writer ContentWriter, raw json.RawMessage) (profile ContentID, records []ContentID, err error) {
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
		records = make([]ContentID, 0, len(parts))
		for index, item := range parts {
			id, storeErr := StoreResponsesRecord(writer, item)
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
	var request responses.Request
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

// ResponsesContentReferences returns the child content IDs encoded by one of the three
// Responses storage envelopes. It deliberately does not inspect provider JSON
// kept in an envelope: only fields owned by these typed storage records are
// graph edges.
func ResponsesContentReferences(kind ContentKind, payload json.RawMessage) ([]ContentID, error) {
	var refs []ContentID
	add := func(id ContentID) error {
		if id == "" {
			return fmt.Errorf("stored Responses %s has an empty content reference: %w", kind, protocol.ErrInvalid)
		}
		refs = append(refs, id)
		return nil
	}

	switch kind {
	case ContentOpenAIItem:
		var stored storedItem
		if err := json.Unmarshal(payload, &stored); err != nil {
			return nil, fmt.Errorf("decode stored Responses item references: %w", err)
		}
		if stored.Type == "" {
			return nil, fmt.Errorf("stored Responses item has no type: %w", protocol.ErrInvalid)
		}
		for _, ids := range []*[]ContentID{stored.Content, stored.Summary, stored.Output} {
			if ids == nil {
				continue
			}
			for _, id := range *ids {
				if err := add(id); err != nil {
					return nil, err
				}
			}
		}
		for _, id := range []*ContentID{stored.Input, stored.Arguments, stored.OutputText, stored.ContentText, stored.EncryptedContent} {
			if id != nil {
				if err := add(*id); err != nil {
					return nil, err
				}
			}
		}
	case ContentOpenAIContent:
		var stored storedPart
		if err := json.Unmarshal(payload, &stored); err != nil {
			return nil, fmt.Errorf("decode stored Responses content references: %w", err)
		}
		if len(stored.Raw) != 0 {
			if !json.Valid(stored.Raw) || stored.Type != nil || len(stored.Envelope) != 0 || stored.Text != nil || stored.ImageData != nil {
				return nil, fmt.Errorf("stored raw Responses content part has extracted fields: %w", protocol.ErrInvalid)
			}
			if _, err := rawObject(stored.Raw); err == nil {
				return nil, fmt.Errorf("stored raw Responses content part must be non-object JSON: %w", protocol.ErrInvalid)
			}
			return nil, nil
		}
		if stored.Text != nil {
			if err := add(*stored.Text); err != nil {
				return nil, err
			}
		}
		if stored.ImageData != nil {
			if err := add(stored.ImageData.Ref); err != nil {
				return nil, err
			}
		}
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
	writer ContentWriter,
	request responses.Request,
	toolRecords []json.RawMessage,
	envelope map[string]json.RawMessage,
) (ContentID, error) {
	instructionID, err := putString(writer, ContentText, request.Instructions)
	if err != nil {
		return "", fmt.Errorf("store request instructions: %w", err)
	}
	toolIDs := make([]ContentID, 0, len(toolRecords))
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

// RestoreResponsesRequest restores a request profile and inserts the already-restored
// native item JSON records in their original order.
func RestoreResponsesRequest(reader ContentReader, profile ContentID, records []json.RawMessage) ([]byte, error) {
	payload, err := getPayload(reader, profile, ContentOpenAIRequest)
	if err != nil {
		return nil, err
	}
	var stored storedRequestProfile
	if err := json.Unmarshal(payload, &stored); err != nil {
		return nil, fmt.Errorf("decode stored Responses request profile: %w", err)
	}
	instructions, err := restoreString(reader, stored.Instructions, ContentText)
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
	request := responses.Request{
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
	responses.Request
	Input []json.RawMessage `json:"input"`
	Tools []json.RawMessage `json:"tools,omitempty"`
}

func storeParts(writer ContentWriter, parts []json.RawMessage) ([]ContentID, error) {
	refs := make([]ContentID, 0, len(parts))
	for _, part := range parts {
		payload, err := storePart(writer, part)
		if err != nil {
			return nil, err
		}
		refs = append(refs, payload)
	}
	return refs, nil
}

func storePart(writer ContentWriter, raw json.RawMessage) (ContentID, error) {
	fields, err := rawObject(raw)
	if err != nil {
		if !json.Valid(raw) {
			return "", err
		}
		payload, marshalErr := json.Marshal(storedPart{Raw: protocol.CloneJSON(raw)})
		if marshalErr != nil {
			return "", marshalErr
		}
		return writer.PutContent(ContentOpenAIContent, payload)
	}
	stored := storedPart{}
	if typeRaw, present := fields["type"]; present {
		if typeValue, ok := jsonString(typeRaw); ok {
			stored.Type = &typeValue
			delete(fields, "type")
		}
	}
	if textRaw, present := fields["text"]; present {
		if textValue, ok := jsonString(textRaw); ok {
			id, err := putString(writer, ContentText, textValue)
			if err != nil {
				return "", err
			}
			stored.Text = &id
			delete(fields, "text")
		}
	}
	for _, field := range []string{"image_url", "data"} {
		if imageRaw, present := fields[field]; present {
			if imageValue, ok := jsonString(imageRaw); ok {
				if prefix, data, isDataURL := splitDataURL(imageValue); isDataURL {
					id, err := putString(writer, ContentImageData, data)
					if err != nil {
						return "", err
					}
					stored.ImageData = &storedImageData{Field: field, Prefix: prefix, Ref: id}
					delete(fields, field)
					break
				}
			}
		}
	}
	if len(fields) != 0 {
		stored.Envelope = fields
	}
	payload, err := json.Marshal(stored)
	if err != nil {
		return "", err
	}
	return writer.PutContent(ContentOpenAIContent, payload)
}

func restorePart(reader ContentReader, id ContentID) (json.RawMessage, error) {
	payload, err := getPayload(reader, id, ContentOpenAIContent)
	if err != nil {
		return nil, err
	}
	var stored storedPart
	if err := json.Unmarshal(payload, &stored); err != nil {
		return nil, fmt.Errorf("decode stored Responses content part: %w", err)
	}
	if len(stored.Raw) != 0 {
		if stored.Type != nil || len(stored.Envelope) != 0 || stored.Text != nil || stored.ImageData != nil {
			return nil, fmt.Errorf("stored raw Responses content part has extracted fields: %w", protocol.ErrInvalid)
		}
		if _, err := rawObject(stored.Raw); err == nil {
			return nil, fmt.Errorf("stored raw Responses content part must be non-object JSON: %w", protocol.ErrInvalid)
		}
		return protocol.CloneJSON(stored.Raw), nil
	}
	fields := cloneEnvelope(stored.Envelope)
	if stored.Type != nil {
		if err := setField(fields, "type", *stored.Type); err != nil {
			return nil, err
		}
	}
	if stored.Text != nil {
		text, err := restoreString(reader, *stored.Text, ContentText)
		if err != nil {
			return nil, fmt.Errorf("restore part text: %w", err)
		}
		if err := setField(fields, "text", text); err != nil {
			return nil, err
		}
	}
	if stored.ImageData != nil {
		data, err := restoreString(reader, stored.ImageData.Ref, ContentImageData)
		if err != nil {
			return nil, fmt.Errorf("restore image data: %w", err)
		}
		if !strings.HasPrefix(stored.ImageData.Prefix, "data:") || !strings.Contains(stored.ImageData.Prefix, ";base64,") {
			return nil, fmt.Errorf("invalid stored image data prefix: %w", protocol.ErrInvalid)
		}
		if stored.ImageData.Field != "image_url" && stored.ImageData.Field != "data" {
			return nil, fmt.Errorf("invalid stored image data field %q: %w", stored.ImageData.Field, protocol.ErrInvalid)
		}
		if err := setField(fields, stored.ImageData.Field, stored.ImageData.Prefix+data); err != nil {
			return nil, err
		}
	}
	return json.Marshal(fields)
}

func putString(writer ContentWriter, kind ContentKind, value string) (ContentID, error) {
	payload, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	return writer.PutContent(kind, payload)
}

func restoreString(reader ContentReader, id ContentID, kind ContentKind) (string, error) {
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

func getPayload(reader ContentReader, id ContentID, kind ContentKind) (json.RawMessage, error) {
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

func jsonString(raw json.RawMessage) (string, bool) {
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		return "", false
	}
	return value, true
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

func splitDataURL(value string) (prefix, data string, ok bool) {
	if !strings.HasPrefix(value, "data:") {
		return "", "", false
	}
	comma := strings.IndexByte(value, ',')
	if comma < 0 {
		return "", "", false
	}
	prefix = value[:comma+1]
	if !strings.Contains(prefix, ";base64,") {
		return "", "", false
	}
	return prefix, value[comma+1:], true
}

func cloneEnvelope(envelope map[string]json.RawMessage) map[string]json.RawMessage {
	fields := make(map[string]json.RawMessage, len(envelope)+8)
	for name, value := range envelope {
		fields[name] = protocol.CloneJSON(value)
	}
	return fields
}

func setField(fields map[string]json.RawMessage, name string, value any) error {
	if _, exists := fields[name]; exists {
		return fmt.Errorf("stored Responses envelope duplicates extracted field %q: %w", name, protocol.ErrInvalid)
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		return err
	}
	fields[name] = encoded
	return nil
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
