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
	Model           string         `json:"model"`
	ReasoningEffort string         `json:"reasoning_effort,omitempty"`
	Tools           []tool         `json:"tools,omitempty"`
	Stream          bool           `json:"stream"`
	ToolChoice      string         `json:"tool_choice,omitempty"`
	MaxTokens       int            `json:"max_tokens,omitempty"`
	StreamOptions   *streamOptions `json:"stream_options,omitempty"`
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
func PrepareRequest(store content.ContentWriter, model, effort string, instructions *string, surface protocol.ToolSurface) (content.ContentID, content.ContentID, error) {
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
		Model: model, ReasoningEffort: effort, Stream: true, StreamOptions: &streamOptions{IncludeUsage: true},
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

// PrepareNextProfile updates the retained profile for new requests. Already
// committed attempts still render their original profile on retry or inspection.
func PrepareNextProfile(store interface {
	content.ContentReader
	content.ContentWriter
}, id content.ContentID, model, effort string,
) (content.ContentID, error) {
	raw, err := load(store, id, profileKind)
	if err != nil {
		return "", err
	}
	var profile requestProfile
	if err := json.Unmarshal(raw, &profile); err != nil {
		return "", err
	}
	if profile.StreamOptions != nil && profile.StreamOptions.IncludeUsage && profile.ReasoningEffort == effort && profile.Model == model {
		return id, nil
	}
	profile.StreamOptions = &streamOptions{IncludeUsage: true}
	profile.ReasoningEffort = effort
	profile.Model = model
	raw, err = json.Marshal(profile)
	if err != nil {
		return "", err
	}
	return store.PutContent(profileKind, raw)
}

// PrepareRequest retains the system and tool profile and stores only the prefix being summarized.
func (p *CompactionPlan) PrepareRequest(store interface {
	content.ContentReader
	content.ContentWriter
}, profileID content.ContentID,
) (content.ContentID, []content.ContentID, content.ContentID, error) {
	raw, err := load(store, profileID, profileKind)
	if err != nil {
		return "", nil, "", err
	}
	var profile requestProfile
	if err := json.Unmarshal(raw, &profile); err != nil {
		return "", nil, "", err
	}
	profile.ToolChoice = "none"
	profile.MaxTokens = p.config.MaxTokens
	raw, err = json.Marshal(profile)
	if err != nil {
		return "", nil, "", err
	}
	profileID, err = store.PutContent(profileKind, raw)
	if err != nil {
		return "", nil, "", err
	}
	instruction, err := p.config.summaryPrompt()
	if err != nil {
		return "", nil, "", err
	}
	raw, err = json.Marshal(message{Role: "user", Content: instruction})
	if err != nil {
		return "", nil, "", err
	}
	instructionID, err := StoreRecord(store, raw)
	if err != nil {
		return "", nil, "", err
	}
	records, err := (&Context{messages: p.prefix}).ProjectRecords(0)
	if err != nil {
		return "", nil, "", err
	}
	ids := make([]content.ContentID, 0, len(records))
	for _, record := range records {
		id, err := StoreRecord(store, record)
		if err != nil {
			return "", nil, "", err
		}
		ids = append(ids, id)
	}
	return profileID, ids, instructionID, nil
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
