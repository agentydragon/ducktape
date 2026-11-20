package chatcompletions

import (
	"encoding/json"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

const (
	messageKind content.ContentKind = "chat_completions_message"
	profileKind content.ContentKind = "chat_completions_request"
)

type requestProfile struct {
	Model         string         `json:"model"`
	Tools         []tool         `json:"tools,omitempty"`
	Stream        bool           `json:"stream"`
	ToolChoice    string         `json:"tool_choice,omitempty"`
	MaxTokens     int            `json:"max_tokens,omitempty"`
	StreamOptions *streamOptions `json:"stream_options,omitempty"`
}

type streamOptions struct {
	IncludeUsage bool `json:"include_usage"`
}

type tool struct {
	Type     string         `json:"type"`
	Function toolDefinition `json:"function"`
}

type toolDefinition struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Parameters  json.RawMessage `json:"parameters"`
}

// PrepareRequest stores the reusable request options and the system-message prefix.
func PrepareRequest(store content.ContentWriter, model string, instructions *string, surface protocol.ToolSurface) (content.ContentID, content.ContentID, error) {
	if !json.Valid(surface.Parameters) {
		return "", "", protocol.ErrInvalid
	}
	system := protocol.BaseInstructions
	if instructions != nil && *instructions != "" {
		system += "\n\n" + *instructions
	}
	systemRecord, err := json.Marshal(message{Role: "system", Content: system})
	if err != nil {
		return "", "", err
	}
	profile, err := json.Marshal(requestProfile{
		Model: model, Stream: true, StreamOptions: &streamOptions{IncludeUsage: true},
		Tools: []tool{{Type: "function", Function: toolDefinition{Name: "exec", Description: surface.Description, Parameters: surface.Parameters}}},
	})
	if err != nil {
		return "", "", err
	}
	profileID, err := store.PutContent(profileKind, profile)
	if err != nil {
		return "", "", err
	}
	systemID, err := StoreRecord(store, systemRecord)
	return profileID, systemID, err
}

// WithStreamUsage upgrades a retained sampling profile for new requests. Already
// committed attempts still render their original profile on retry or inspection.
func WithStreamUsage(store interface {
	content.ContentReader
	content.ContentWriter
}, id content.ContentID,
) (content.ContentID, error) {
	raw, err := load(store, id, profileKind)
	if err != nil {
		return "", err
	}
	var profile requestProfile
	if err := json.Unmarshal(raw, &profile); err != nil {
		return "", err
	}
	if profile.StreamOptions != nil && profile.StreamOptions.IncludeUsage {
		return id, nil
	}
	profile.StreamOptions = &streamOptions{IncludeUsage: true}
	raw, err = json.Marshal(profile)
	if err != nil {
		return "", err
	}
	return store.PutContent(profileKind, raw)
}

// PrepareCompactionRequest keeps the sampling tool definitions in the prompt and
// disables their selection for the appended summary request.
func PrepareCompactionRequest(store interface {
	content.ContentReader
	content.ContentWriter
}, profileID content.ContentID, config CompactionConfig,
) (content.ContentID, content.ContentID, error) {
	raw, err := load(store, profileID, profileKind)
	if err != nil {
		return "", "", err
	}
	var profile requestProfile
	if err := json.Unmarshal(raw, &profile); err != nil {
		return "", "", err
	}
	profile.ToolChoice = "none"
	profile.MaxTokens = config.MaxTokens
	raw, err = json.Marshal(profile)
	if err != nil {
		return "", "", err
	}
	profileID, err = store.PutContent(profileKind, raw)
	if err != nil {
		return "", "", err
	}
	instruction := fmt.Sprintf("%s The last %d user turns before this request will be retained verbatim. Focus the summary on the earlier turns.", summaryInstructions, config.KeepRecentTurns)
	raw, err = json.Marshal(message{Role: "user", Content: instruction})
	if err != nil {
		return "", "", err
	}
	instructionID, err := StoreRecord(store, raw)
	return profileID, instructionID, err
}

func StoreRecord(store content.ContentWriter, raw json.RawMessage) (content.ContentID, error) {
	return store.PutContent(messageKind, raw)
}

func RestoreRecord(store content.ContentReader, id content.ContentID) (json.RawMessage, error) {
	return load(store, id, messageKind)
}

func RestoreRequest(store content.ContentReader, id content.ContentID, records []json.RawMessage) ([]byte, error) {
	raw, err := load(store, id, profileKind)
	if err != nil {
		return nil, err
	}
	var profile requestProfile
	if err := json.Unmarshal(raw, &profile); err != nil {
		return nil, err
	}
	return json.Marshal(struct {
		requestProfile
		Messages []json.RawMessage `json:"messages"`
	}{profile, records})
}

func load(store content.ContentReader, id content.ContentID, kind content.ContentKind) (json.RawMessage, error) {
	row, err := store.GetContent(id)
	if err != nil {
		return nil, err
	}
	if row.Kind != kind {
		return nil, fmt.Errorf("stored content %s has kind %q, expected %q", id, row.Kind, kind)
	}
	return row.Payload, nil
}
