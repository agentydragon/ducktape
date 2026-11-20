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
	Model  string `json:"model"`
	Tools  []tool `json:"tools"`
	Stream bool   `json:"stream"`
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
		Model: model, Stream: true,
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
