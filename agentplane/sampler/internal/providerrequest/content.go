package providerrequest

import (
	"encoding/json"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/chatcompletions"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
)

func StoreRecord(s content.ContentWriter, api protocol.APIStyle, record json.RawMessage) (content.ContentID, error) {
	switch api {
	case protocol.APIStyleResponses:
		return responses.StoreRecord(s, record)
	case protocol.APIStyleAnthropicMessages:
		return anthropic.StoreRecord(s, record)
	case protocol.APIStyleChatCompletions:
		return chatcompletions.StoreRecord(s, record)
	default:
		return "", protocol.ErrInvalid
	}
}

// ContentReferences returns the stored content IDs directly referenced by one row.
// Provider codecs own the stored formats; persistence callers only need the shared
// graph traversal contract.
func ContentReferences(kind content.ContentKind, payload json.RawMessage) ([]content.ContentID, error) {
	switch kind {
	case responses.ContentOpenAIRequest:
		return responses.ContentReferences(kind, payload)
	case anthropic.ContentAnthropicMessage, anthropic.ContentAnthropicRequest:
		return anthropic.ContentReferences(kind, payload)
	default:
		return nil, nil
	}
}

func RestoreRecords(s content.ContentReader, api protocol.APIStyle, ids []content.ContentID) ([]json.RawMessage, error) {
	records := make([]json.RawMessage, 0, len(ids))
	for _, id := range ids {
		var record json.RawMessage
		var err error
		switch api {
		case protocol.APIStyleResponses:
			record, err = responses.RestoreRecord(s, id)
		case protocol.APIStyleAnthropicMessages:
			record, err = anthropic.RestoreRecord(s, id)
		case protocol.APIStyleChatCompletions:
			record, err = chatcompletions.RestoreRecord(s, id)
		default:
			return nil, protocol.ErrInvalid
		}
		if err != nil {
			return nil, err
		}
		records = append(records, record)
	}
	return records, nil
}

func Render(store content.ContentReader, api protocol.APIStyle, profile content.ContentID, ids []content.ContentID) ([]byte, error) {
	records, err := RestoreRecords(store, api, ids)
	if err != nil {
		return nil, err
	}
	switch api {
	case protocol.APIStyleResponses:
		return responses.RestoreRequest(store, profile, records)
	case protocol.APIStyleAnthropicMessages:
		return anthropic.RestoreRequest(store, profile, records)
	case protocol.APIStyleChatCompletions:
		return chatcompletions.RestoreRequest(store, profile, records)
	default:
		return nil, protocol.ErrInvalid
	}
}
