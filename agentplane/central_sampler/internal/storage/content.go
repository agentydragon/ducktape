package storage

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

type (
	ContentID   string
	ContentKind string
)

const (
	ContentText                      ContentKind = "text"
	ContentImageData                 ContentKind = "image_data"
	ContentOpaque                    ContentKind = "opaque_continuation"
	ContentOpenAIItem                ContentKind = "openai_item"
	ContentOpenAIContent             ContentKind = "openai_content"
	ContentOpenAIRequest             ContentKind = "openai_request"
	ContentOpenAITool                ContentKind = "openai_tool"
	ContentAnthropicMessage          ContentKind = "anthropic_message"
	ContentAnthropicText             ContentKind = "anthropic_text"
	ContentAnthropicThinking         ContentKind = "anthropic_thinking"
	ContentAnthropicRedactedThinking ContentKind = "anthropic_redacted_thinking"
	ContentAnthropicToolUse          ContentKind = "anthropic_tool_use"
	ContentAnthropicToolResult       ContentKind = "anthropic_tool_result"
	ContentAnthropicImage            ContentKind = "anthropic_image"
	ContentAnthropicCompaction       ContentKind = "anthropic_compaction"
	ContentAnthropicRequest          ContentKind = "anthropic_request"
	ContentAnthropicTool             ContentKind = "anthropic_tool"
)

func (kind ContentKind) Valid() bool {
	switch kind {
	case ContentText, ContentImageData, ContentOpaque, ContentOpenAIItem, ContentOpenAIContent, ContentOpenAIRequest, ContentOpenAITool, ContentAnthropicMessage,
		ContentAnthropicText, ContentAnthropicThinking, ContentAnthropicRedactedThinking, ContentAnthropicToolUse,
		ContentAnthropicToolResult, ContentAnthropicImage, ContentAnthropicCompaction, ContentAnthropicRequest, ContentAnthropicTool:
		return true
	default:
		return false
	}
}

type StoredContent struct {
	ID      ContentID       `json:"id"`
	Kind    ContentKind     `json:"kind"`
	Payload json.RawMessage `json:"payload"`
}

type ContentWriter interface {
	PutContent(ContentKind, json.RawMessage) (ContentID, error)
}

type ContentReader interface {
	GetContent(ContentID) (StoredContent, error)
}

// CanonicalContent uses the standard JSON codec; numeric tokens retain their precision.
func CanonicalContent(kind ContentKind, payload json.RawMessage) (StoredContent, error) {
	if !kind.Valid() {
		return StoredContent{}, fmt.Errorf("unknown content kind %q", kind)
	}
	value, err := protocol.DecodeJSONValue(payload)
	if err != nil {
		return StoredContent{}, err
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		return StoredContent{}, err
	}
	digest := sha256.New()
	digest.Write([]byte("agentplane/provider-content/v1\x00"))
	digest.Write([]byte(kind))
	digest.Write([]byte{0})
	digest.Write(canonical)
	return StoredContent{ID: ContentID(hex.EncodeToString(digest.Sum(nil))), Kind: kind, Payload: canonical}, nil
}

func VerifyContent(content StoredContent) error {
	expected, err := CanonicalContent(content.Kind, content.Payload)
	if err != nil {
		return err
	}
	if expected.ID != content.ID {
		return fmt.Errorf("content hash mismatch for %s", content.ID)
	}
	return nil
}
