package storage

import (
	"bytes"
	"encoding/json"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

type contentMode string

const (
	contentModeNone   contentMode = ""
	contentModeAbsent contentMode = "absent"
	contentModeArray  contentMode = "array"
	contentModeText   contentMode = "text"
	contentModeInline contentMode = "inline"
	contentModeNull   contentMode = "null"
)

// messageContent stores the provider's envelope fields and references each ordered content block.
// Fields are provider field names with their original JSON values; keeping these as RawMessage
// preserves numbers and metadata the sampler does not interpret.
type messageContent struct {
	Fields      map[string]json.RawMessage `json:"fields"`
	ContentMode contentMode                `json:"content_mode"`
	Blocks      []ContentID                `json:"blocks,omitempty"`
	TextID      ContentID                  `json:"text_id,omitempty"`
}

// blockContent is the typed storage envelope for Anthropic content blocks. Large native scalar
// values and nested tool-result content are references; all other provider fields stay raw JSON.
type blockContent struct {
	Type        anthropic.BlockType        `json:"type"`
	Fields      map[string]json.RawMessage `json:"fields,omitempty"`
	ScalarField string                     `json:"scalar_field,omitempty"`
	ScalarID    ContentID                  `json:"scalar_id,omitempty"`
	ContentMode contentMode                `json:"content_mode,omitempty"`
	Blocks      []ContentID                `json:"blocks,omitempty"`
	Source      *imageSourceContent        `json:"source,omitempty"`
}

type imageSourceContent struct {
	Fields map[string]json.RawMessage `json:"fields"`
	DataID ContentID                  `json:"data_id"`
}

// requestContent removes the repetitive system prompt and tool definitions from a request
// profile. Message records are returned separately in their original order.
type requestContent struct {
	Fields       map[string]json.RawMessage `json:"fields"`
	SystemID     ContentID                  `json:"system_id,omitempty"`
	SystemMode   contentMode                `json:"system_mode"`
	Tools        []ContentID                `json:"tools,omitempty"`
	ToolsMode    contentMode                `json:"tools_mode"`
	MessagesMode contentMode                `json:"messages_mode"`
}

type requestMetadata struct {
	CacheControl *anthropic.CacheControl        `json:"cache_control,omitempty"`
	Compaction   *anthropic.CompactionDirective `json:"compaction,omitempty"`
	Diagnostics  *anthropic.Diagnostics         `json:"diagnostics,omitempty"`
	MaxTokens    int                            `json:"max_tokens"`
	Model        string                         `json:"model"`
	OutputConfig *anthropic.OutputConfig        `json:"output_config,omitempty"`
	Stream       bool                           `json:"stream"`
	Thinking     *anthropic.ThinkingConfig      `json:"thinking,omitempty"`
}

// StoreAnthropicRecord stores a native Anthropic message or response envelope as typed content rows.
// Content blocks remain ordered, while their large text and image payloads deduplicate by value.
func StoreAnthropicRecord(writer ContentWriter, record json.RawMessage) (ContentID, error) {
	fields, err := decodeObject(record)
	if err != nil {
		return "", fmt.Errorf("decode Anthropic record: %w", err)
	}
	content := messageContent{Fields: fields, ContentMode: contentModeAbsent}
	if raw, ok := fields["content"]; ok {
		var blocks []json.RawMessage
		switch {
		case isJSONArray(raw):
			if err := json.Unmarshal(raw, &blocks); err != nil {
				return "", fmt.Errorf("decode Anthropic message content: %w", err)
			}
			content.ContentMode = contentModeArray
			content.Blocks = make([]ContentID, 0, len(blocks))
			for index, block := range blocks {
				id, err := storeBlock(writer, block)
				if err != nil {
					return "", fmt.Errorf("store Anthropic content block %d: %w", index, err)
				}
				content.Blocks = append(content.Blocks, id)
			}
			delete(fields, "content")
		case isJSONString(raw):
			id, err := storeString(writer, ContentText, raw)
			if err != nil {
				return "", fmt.Errorf("store Anthropic message text: %w", err)
			}
			content.ContentMode = contentModeText
			content.TextID = id
			delete(fields, "content")
		default:
			// Preserve provider extensions whose content shape is not understood by this adapter.
			content.ContentMode = contentModeInline
		}
	}
	return putContent(writer, ContentAnthropicMessage, content)
}

// RestoreAnthropicRecord resolves an Anthropic message/response content graph back to provider JSON.
func RestoreAnthropicRecord(reader ContentReader, id ContentID) (json.RawMessage, error) {
	stored, err := loadContent(reader, id, ContentAnthropicMessage)
	if err != nil {
		return nil, err
	}
	var content messageContent
	if err := json.Unmarshal(stored.Payload, &content); err != nil {
		return nil, fmt.Errorf("decode stored Anthropic message %s: %w", id, err)
	}
	fields := cloneFields(content.Fields)
	switch content.ContentMode {
	case contentModeAbsent:
	case contentModeInline:
		if _, ok := fields["content"]; !ok {
			return nil, fmt.Errorf("stored Anthropic message %s has inline content mode without content", id)
		}
	case contentModeText:
		text, err := loadString(reader, content.TextID, ContentText)
		if err != nil {
			return nil, fmt.Errorf("restore Anthropic message text: %w", err)
		}
		fields["content"] = text
	case contentModeArray:
		blocks := make([]json.RawMessage, 0, len(content.Blocks))
		for index, blockID := range content.Blocks {
			block, err := restoreBlock(reader, blockID)
			if err != nil {
				return nil, fmt.Errorf("restore Anthropic content block %d: %w", index, err)
			}
			blocks = append(blocks, block)
		}
		raw, err := json.Marshal(blocks)
		if err != nil {
			return nil, fmt.Errorf("encode Anthropic content blocks: %w", err)
		}
		fields["content"] = raw
	default:
		return nil, fmt.Errorf("stored Anthropic message %s has unknown content mode %q", id, content.ContentMode)
	}
	raw, err := json.Marshal(fields)
	if err != nil {
		return nil, fmt.Errorf("encode Anthropic message %s: %w", id, err)
	}
	return raw, nil
}

// StoreAnthropicRequest stores a native Anthropic request as a reusable profile plus ordered messages.
// Diagnostics and other per-call fields stay on the profile, while system and tools are shared.
func StoreAnthropicRequest(writer ContentWriter, request anthropic.Request) (ContentID, []ContentID, error) {
	encoded, err := json.Marshal(requestMetadata{
		CacheControl: request.CacheControl,
		Compaction:   request.Compaction,
		Diagnostics:  request.Diagnostics,
		MaxTokens:    request.MaxTokens,
		Model:        request.Model,
		OutputConfig: request.OutputConfig,
		Stream:       request.Stream,
		Thinking:     request.Thinking,
	})
	if err != nil {
		return "", nil, fmt.Errorf("encode Anthropic request metadata: %w", err)
	}
	fields, err := decodeObject(encoded)
	if err != nil {
		return "", nil, fmt.Errorf("decode Anthropic request metadata: %w", err)
	}
	systemID, err := putContent(writer, ContentText, request.System)
	if err != nil {
		return "", nil, fmt.Errorf("store Anthropic system prompt: %w", err)
	}
	toolIDs := make([]ContentID, 0, len(request.Tools))
	for index, tool := range request.Tools {
		toolJSON, err := json.Marshal(tool)
		if err != nil {
			return "", nil, fmt.Errorf("encode Anthropic tool %d: %w", index, err)
		}
		id, err := putRawContent(writer, ContentAnthropicTool, toolJSON)
		if err != nil {
			return "", nil, fmt.Errorf("store Anthropic tool %d: %w", index, err)
		}
		toolIDs = append(toolIDs, id)
	}
	messageIDs := make([]ContentID, 0, len(request.Messages))
	for index, message := range request.Messages {
		id, err := StoreAnthropicRecord(writer, message)
		if err != nil {
			return "", nil, fmt.Errorf("store Anthropic request message %d: %w", index, err)
		}
		messageIDs = append(messageIDs, id)
	}
	toolsMode, messagesMode := contentModeArray, contentModeArray
	if request.Tools == nil {
		toolsMode = contentModeNull
	}
	if request.Messages == nil {
		messagesMode = contentModeNull
	}
	profile, err := putContent(writer, ContentAnthropicRequest, requestContent{
		Fields: fields, SystemID: systemID, SystemMode: contentModeText,
		Tools: toolIDs, ToolsMode: toolsMode, MessagesMode: messagesMode,
	})
	if err != nil {
		return "", nil, fmt.Errorf("store Anthropic request profile: %w", err)
	}
	return profile, messageIDs, nil
}

// StoreAnthropicRequestJSON converts a historical native request object to the shared profile format.
// The raw request path retains unknown envelope and tool fields without decoding them into the
// current Request struct.
func StoreAnthropicRequestJSON(writer ContentWriter, raw json.RawMessage) (ContentID, []ContentID, error) {
	nativeFields, err := decodeObject(raw)
	if err != nil {
		return "", nil, fmt.Errorf("decode historical Anthropic request: %w", err)
	}
	content := requestContent{Fields: cloneFields(nativeFields)}

	if system, ok := nativeFields["system"]; ok && isJSONString(system) {
		content.SystemID, err = storeString(writer, ContentText, system)
		if err != nil {
			return "", nil, fmt.Errorf("store historical Anthropic system prompt: %w", err)
		}
		content.SystemMode = contentModeText
		delete(content.Fields, "system")
	} else if _, ok := nativeFields["system"]; ok {
		content.SystemMode = contentModeInline
	} else {
		content.SystemMode = contentModeAbsent
	}

	toolIDs, toolsMode, err := storeHistoricalTools(writer, nativeFields, content.Fields)
	if err != nil {
		return "", nil, err
	}
	content.Tools, content.ToolsMode = toolIDs, toolsMode
	messageIDs, messagesMode, err := storeHistoricalMessages(writer, nativeFields, content.Fields)
	if err != nil {
		return "", nil, err
	}
	content.MessagesMode = messagesMode
	profile, err := putContent(writer, ContentAnthropicRequest, content)
	if err != nil {
		return "", nil, fmt.Errorf("store historical Anthropic request profile: %w", err)
	}
	return profile, messageIDs, nil
}

// RestoreAnthropicRequest reconstructs a request from its shared profile and already-restored messages.
func RestoreAnthropicRequest(reader ContentReader, profile ContentID, records []json.RawMessage) ([]byte, error) {
	stored, err := loadContent(reader, profile, ContentAnthropicRequest)
	if err != nil {
		return nil, err
	}
	var content requestContent
	if err := json.Unmarshal(stored.Payload, &content); err != nil {
		return nil, fmt.Errorf("decode stored Anthropic request profile %s: %w", profile, err)
	}
	fields := cloneFields(content.Fields)
	if content.SystemMode == contentModeText {
		system, err := loadString(reader, content.SystemID, ContentText)
		if err != nil {
			return nil, fmt.Errorf("restore Anthropic system prompt: %w", err)
		}
		fields["system"] = system
	} else if content.SystemMode != contentModeAbsent && content.SystemMode != contentModeInline {
		return nil, fmt.Errorf("stored Anthropic request has unknown system mode %q", content.SystemMode)
	}
	switch content.ToolsMode {
	case contentModeAbsent, contentModeInline:
	case contentModeNull:
		fields["tools"] = json.RawMessage("null")
	case contentModeArray:
		tools := make([]json.RawMessage, 0, len(content.Tools))
		for index, toolID := range content.Tools {
			tool, err := loadContent(reader, toolID, ContentAnthropicTool)
			if err != nil {
				return nil, fmt.Errorf("restore Anthropic tool %d: %w", index, err)
			}
			tools = append(tools, append(json.RawMessage(nil), tool.Payload...))
		}
		encodedTools, err := json.Marshal(tools)
		if err != nil {
			return nil, fmt.Errorf("encode restored Anthropic tools: %w", err)
		}
		fields["tools"] = encodedTools
	default:
		return nil, fmt.Errorf("stored Anthropic request has unknown tools mode %q", content.ToolsMode)
	}
	switch content.MessagesMode {
	case contentModeAbsent, contentModeInline:
	case contentModeNull:
		fields["messages"] = json.RawMessage("null")
	case contentModeArray:
		for index, record := range records {
			if !json.Valid(record) {
				return nil, fmt.Errorf("restored Anthropic request message %d is invalid JSON", index)
			}
		}
		encodedMessages, err := json.Marshal(records)
		if err != nil {
			return nil, fmt.Errorf("encode restored Anthropic messages: %w", err)
		}
		fields["messages"] = encodedMessages
	default:
		return nil, fmt.Errorf("stored Anthropic request has unknown messages mode %q", content.MessagesMode)
	}
	encoded, err := json.Marshal(fields)
	if err != nil {
		return nil, fmt.Errorf("encode restored Anthropic request: %w", err)
	}
	return encoded, nil
}

// AnthropicContentReferences returns the provider-owned children of one stored Anthropic row.
// The typed payloads keep context hydration bounded to a small number of batched reads.
func AnthropicContentReferences(kind ContentKind, payload json.RawMessage) ([]ContentID, error) {
	var refs []ContentID
	appendRef := func(id ContentID, field string) error {
		if id == "" {
			return fmt.Errorf("stored Anthropic %s has no %s reference: %w", kind, field, protocol.ErrInvalid)
		}
		refs = append(refs, id)
		return nil
	}
	appendRefs := func(ids []ContentID, field string) error {
		for index, id := range ids {
			if err := appendRef(id, fmt.Sprintf("%s[%d]", field, index)); err != nil {
				return err
			}
		}
		return nil
	}

	switch kind {
	case ContentAnthropicMessage:
		var content messageContent
		if err := json.Unmarshal(payload, &content); err != nil {
			return nil, fmt.Errorf("decode stored Anthropic message references: %w", err)
		}
		switch content.ContentMode {
		case contentModeAbsent, contentModeInline:
		case contentModeText:
			if err := appendRef(content.TextID, "text"); err != nil {
				return nil, err
			}
		case contentModeArray:
			if err := appendRefs(content.Blocks, "blocks"); err != nil {
				return nil, err
			}
		default:
			return nil, fmt.Errorf("stored Anthropic message has unknown content mode %q: %w", content.ContentMode, protocol.ErrInvalid)
		}
	case ContentAnthropicRequest:
		var content requestContent
		if err := json.Unmarshal(payload, &content); err != nil {
			return nil, fmt.Errorf("decode stored Anthropic request references: %w", err)
		}
		switch content.SystemMode {
		case contentModeAbsent, contentModeInline:
		case contentModeText:
			if err := appendRef(content.SystemID, "system"); err != nil {
				return nil, err
			}
		default:
			return nil, fmt.Errorf("stored Anthropic request has unknown system mode %q: %w", content.SystemMode, protocol.ErrInvalid)
		}
		switch content.ToolsMode {
		case contentModeAbsent, contentModeInline, contentModeNull:
		case contentModeArray:
			if err := appendRefs(content.Tools, "tools"); err != nil {
				return nil, err
			}
		default:
			return nil, fmt.Errorf("stored Anthropic request has unknown tools mode %q: %w", content.ToolsMode, protocol.ErrInvalid)
		}
	case ContentAnthropicText,
		ContentAnthropicThinking,
		ContentAnthropicRedactedThinking,
		ContentAnthropicToolUse,
		ContentAnthropicToolResult,
		ContentAnthropicImage,
		ContentAnthropicCompaction:
		var block blockContent
		if err := json.Unmarshal(payload, &block); err != nil {
			return nil, fmt.Errorf("decode stored Anthropic block references: %w", err)
		}
		blockKind, _ := blockStorageKind(block.Type)
		if blockKind != kind {
			return nil, fmt.Errorf("stored Anthropic block type %q does not match kind %q: %w", block.Type, kind, protocol.ErrInvalid)
		}
		if block.ScalarID != "" {
			if err := appendRef(block.ScalarID, "scalar"); err != nil {
				return nil, err
			}
		}
		switch block.ContentMode {
		case contentModeNone, contentModeText:
		case contentModeArray:
			if err := appendRefs(block.Blocks, "blocks"); err != nil {
				return nil, err
			}
		default:
			return nil, fmt.Errorf("stored Anthropic block has unknown content mode %q: %w", block.ContentMode, protocol.ErrInvalid)
		}
		if block.Source != nil {
			if err := appendRef(block.Source.DataID, "image data"); err != nil {
				return nil, err
			}
		}
	}
	return refs, nil
}

func storeBlock(writer ContentWriter, raw json.RawMessage) (ContentID, error) {
	fields, err := decodeObject(raw)
	if err != nil {
		return "", fmt.Errorf("decode block: %w", err)
	}
	typeValue, _ := jsonString(fields["type"])
	typeName := anthropic.BlockType(typeValue)
	if typeName == "" {
		return "", fmt.Errorf("Anthropic block has no type")
	}
	delete(fields, "type")
	content := blockContent{Type: typeName, Fields: fields}
	kind, scalarField := blockStorageKind(typeName)
	if scalarField != "" {
		if scalar, ok := fields[scalarField]; ok && isJSONString(scalar) {
			payloadKind := ContentText
			if typeName == anthropic.BlockRedactedThinking {
				payloadKind = ContentOpaque
			}
			id, err := storeString(writer, payloadKind, scalar)
			if err != nil {
				return "", fmt.Errorf("store %s payload: %w", typeName, err)
			}
			content.ScalarField = scalarField
			content.ScalarID = id
			delete(fields, scalarField)
		}
	}
	if typeName == anthropic.BlockImage {
		if source, ok := fields["source"]; ok {
			sourceFields, err := decodeObject(source)
			if err == nil {
				if data, ok := sourceFields["data"]; ok && isJSONString(data) {
					id, err := storeString(writer, ContentImageData, data)
					if err != nil {
						return "", fmt.Errorf("store Anthropic image data: %w", err)
					}
					delete(sourceFields, "data")
					content.Source = &imageSourceContent{Fields: sourceFields, DataID: id}
					delete(fields, "source")
				}
			}
		}
	}
	if typeName == anthropic.BlockToolResult {
		if nested, ok := fields["content"]; ok {
			switch {
			case isJSONArray(nested):
				var blocks []json.RawMessage
				if err := json.Unmarshal(nested, &blocks); err != nil {
					return "", fmt.Errorf("decode tool_result content: %w", err)
				}
				content.ContentMode = contentModeArray
				content.Blocks = make([]ContentID, 0, len(blocks))
				for index, block := range blocks {
					id, err := storeBlock(writer, block)
					if err != nil {
						return "", fmt.Errorf("store tool_result content block %d: %w", index, err)
					}
					content.Blocks = append(content.Blocks, id)
				}
				delete(fields, "content")
			case isJSONString(nested):
				id, err := storeString(writer, ContentText, nested)
				if err != nil {
					return "", fmt.Errorf("store tool_result text: %w", err)
				}
				content.ContentMode = contentModeText
				content.ScalarID = id
				delete(fields, "content")
			}
		}
	}
	return putContent(writer, kind, content)
}

func restoreBlock(reader ContentReader, id ContentID) (json.RawMessage, error) {
	stored, err := reader.GetContent(id)
	if err != nil {
		return nil, fmt.Errorf("load Anthropic block %s: %w", id, err)
	}
	if err := VerifyContent(stored); err != nil {
		return nil, err
	}
	var block blockContent
	if err := json.Unmarshal(stored.Payload, &block); err != nil {
		return nil, fmt.Errorf("decode stored Anthropic block %s: %w", id, err)
	}
	expectedKind, scalarField := blockStorageKind(block.Type)
	if stored.Kind != expectedKind {
		return nil, fmt.Errorf("stored Anthropic block %s has type %q in kind %q", id, block.Type, stored.Kind)
	}
	fields := cloneFields(block.Fields)
	if block.ScalarID != "" && block.ScalarField != "" {
		payloadKind := ContentText
		if block.Type == anthropic.BlockRedactedThinking {
			payloadKind = ContentOpaque
		}
		scalar, err := loadString(reader, block.ScalarID, payloadKind)
		if err != nil {
			return nil, fmt.Errorf("restore %s payload: %w", block.Type, err)
		}
		field := block.ScalarField
		if field == "" {
			field = scalarField
		}
		fields[field] = scalar
	}
	if block.Source != nil {
		data, err := loadString(reader, block.Source.DataID, ContentImageData)
		if err != nil {
			return nil, fmt.Errorf("restore Anthropic image data: %w", err)
		}
		sourceFields := cloneFields(block.Source.Fields)
		sourceFields["data"] = data
		source, err := json.Marshal(sourceFields)
		if err != nil {
			return nil, fmt.Errorf("encode Anthropic image source: %w", err)
		}
		fields["source"] = source
	}
	if block.Type == anthropic.BlockToolResult && block.ContentMode != contentModeNone {
		switch block.ContentMode {
		case contentModeText:
			text, err := loadString(reader, block.ScalarID, ContentText)
			if err != nil {
				return nil, fmt.Errorf("restore tool_result text: %w", err)
			}
			fields["content"] = text
		case contentModeArray:
			children := make([]json.RawMessage, 0, len(block.Blocks))
			for index, childID := range block.Blocks {
				child, err := restoreBlock(reader, childID)
				if err != nil {
					return nil, fmt.Errorf("restore tool_result content block %d: %w", index, err)
				}
				children = append(children, child)
			}
			content, err := json.Marshal(children)
			if err != nil {
				return nil, fmt.Errorf("encode tool_result content: %w", err)
			}
			fields["content"] = content
		default:
			return nil, fmt.Errorf("stored tool_result has unknown content mode %q", block.ContentMode)
		}
	}
	typeRaw, err := json.Marshal(block.Type)
	if err != nil {
		return nil, fmt.Errorf("encode Anthropic block type: %w", err)
	}
	fields["type"] = typeRaw
	raw, err := json.Marshal(fields)
	if err != nil {
		return nil, fmt.Errorf("encode Anthropic block: %w", err)
	}
	return raw, nil
}

func blockStorageKind(blockType anthropic.BlockType) (ContentKind, string) {
	switch blockType {
	case anthropic.BlockText:
		return ContentAnthropicText, "text"
	case anthropic.BlockThinking:
		return ContentAnthropicThinking, "thinking"
	case anthropic.BlockRedactedThinking:
		return ContentAnthropicRedactedThinking, "data"
	case anthropic.BlockToolUse:
		return ContentAnthropicToolUse, ""
	case anthropic.BlockToolResult:
		return ContentAnthropicToolResult, ""
	case anthropic.BlockImage:
		return ContentAnthropicImage, ""
	case anthropic.BlockCompaction:
		return ContentAnthropicCompaction, "content"
	default:
		return "", ""
	}
}

func putContent(writer ContentWriter, kind ContentKind, value any) (ContentID, error) {
	payload, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	return putRawContent(writer, kind, payload)
}

func putRawContent(writer ContentWriter, kind ContentKind, payload json.RawMessage) (ContentID, error) {
	return writer.PutContent(kind, payload)
}

func storeHistoricalTools(writer ContentWriter, source, profile map[string]json.RawMessage) ([]ContentID, contentMode, error) {
	raw, ok := source["tools"]
	if !ok {
		return nil, contentModeAbsent, nil
	}
	if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		delete(profile, "tools")
		return nil, contentModeNull, nil
	}
	if !isJSONArray(raw) {
		return nil, contentModeInline, nil
	}
	var tools []json.RawMessage
	if err := json.Unmarshal(raw, &tools); err != nil {
		return nil, "", fmt.Errorf("decode historical Anthropic tools: %w", err)
	}
	ids := make([]ContentID, 0, len(tools))
	for index, tool := range tools {
		if _, err := decodeObject(tool); err != nil {
			return nil, "", fmt.Errorf("decode historical Anthropic tool %d: %w", index, err)
		}
		id, err := putRawContent(writer, ContentAnthropicTool, tool)
		if err != nil {
			return nil, "", fmt.Errorf("store historical Anthropic tool %d: %w", index, err)
		}
		ids = append(ids, id)
	}
	delete(profile, "tools")
	return ids, contentModeArray, nil
}

func storeHistoricalMessages(writer ContentWriter, source, profile map[string]json.RawMessage) ([]ContentID, contentMode, error) {
	raw, ok := source["messages"]
	if !ok {
		return nil, contentModeAbsent, nil
	}
	if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		delete(profile, "messages")
		return nil, contentModeNull, nil
	}
	if !isJSONArray(raw) {
		return nil, contentModeInline, nil
	}
	var messages []json.RawMessage
	if err := json.Unmarshal(raw, &messages); err != nil {
		return nil, "", fmt.Errorf("decode historical Anthropic messages: %w", err)
	}
	ids := make([]ContentID, 0, len(messages))
	for index, message := range messages {
		id, err := StoreAnthropicRecord(writer, message)
		if err != nil {
			return nil, "", fmt.Errorf("store historical Anthropic message %d: %w", index, err)
		}
		ids = append(ids, id)
	}
	delete(profile, "messages")
	return ids, contentModeArray, nil
}

func storeString(writer ContentWriter, kind ContentKind, raw json.RawMessage) (ContentID, error) {
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		return "", err
	}
	return putContent(writer, kind, value)
}

func loadContent(reader ContentReader, id ContentID, kind ContentKind) (StoredContent, error) {
	content, err := reader.GetContent(id)
	if err != nil {
		return StoredContent{}, fmt.Errorf("load content %s: %w", id, err)
	}
	if err := VerifyContent(content); err != nil {
		return StoredContent{}, err
	}
	if content.Kind != kind {
		return StoredContent{}, fmt.Errorf("content %s has kind %q, want %q", id, content.Kind, kind)
	}
	return content, nil
}

func loadString(reader ContentReader, id ContentID, kind ContentKind) (json.RawMessage, error) {
	if id == "" {
		return nil, fmt.Errorf("missing %s content reference", kind)
	}
	content, err := loadContent(reader, id, kind)
	if err != nil {
		return nil, err
	}
	var value string
	if err := json.Unmarshal(content.Payload, &value); err != nil {
		return nil, fmt.Errorf("content %s is not a JSON string: %w", id, err)
	}
	return append(json.RawMessage(nil), content.Payload...), nil
}

func decodeObject(raw []byte) (map[string]json.RawMessage, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return nil, err
	}
	if fields == nil {
		return nil, fmt.Errorf("expected JSON object")
	}
	return fields, nil
}

func cloneFields(fields map[string]json.RawMessage) map[string]json.RawMessage {
	copyOfFields := make(map[string]json.RawMessage, len(fields)+2)
	for name, raw := range fields {
		copyOfFields[name] = append(json.RawMessage(nil), raw...)
	}
	return copyOfFields
}

func isJSONString(raw json.RawMessage) bool {
	trimmed := bytes.TrimSpace(raw)
	return len(trimmed) > 0 && trimmed[0] == '"'
}

func isJSONArray(raw json.RawMessage) bool {
	trimmed := bytes.TrimSpace(raw)
	return len(trimmed) > 0 && trimmed[0] == '['
}
