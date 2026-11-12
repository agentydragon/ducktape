package centralsampler

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/storage"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
	"github.com/google/uuid"
)

func renderProviderRequest(store *contentStore, request ProviderRequest) ([]byte, error) {
	ids, err := store.loadContext(request.ContextID)
	if err != nil {
		return nil, err
	}
	roots := append([]storage.ContentID{request.ProfileID}, ids...)
	if err := store.prefetch(roots); err != nil {
		return nil, err
	}
	records, err := store.restoreRecords(request.API, ids)
	if err != nil {
		return nil, err
	}
	switch request.API {
	case protocol.APIStyleResponses:
		return storage.RestoreResponsesRequest(store, request.ProfileID, records)
	case protocol.APIStyleAnthropicMessages:
		return storage.RestoreAnthropicRequest(store, request.ProfileID, records)
	default:
		return nil, protocol.ErrInvalid
	}
}

func finishStoredRequest(store *contentStore, request ProviderRequest, profile storage.ContentID, ids []storage.ContentID) (ProviderRequest, []byte, error) {
	contextID, err := store.storeContext(request.API, ids)
	if err != nil {
		return ProviderRequest{}, nil, err
	}
	request.ContextID = contextID
	request.ProfileID = profile
	wire, err := renderProviderRequest(store, request)
	return request, wire, err
}

// New requests are packed from native provider structs, before serializing a full body.
func prepareProviderRequest(ctx context.Context, tx *sql.Tx, conversationID string, request ProviderRequest, native any) (ProviderRequest, []byte, error) {
	native, err := freezeProviderInstructions(ctx, tx, conversationID, native)
	if err != nil {
		return ProviderRequest{}, nil, err
	}
	store := newContentStore(ctx, tx)
	var profile storage.ContentID
	var ids []storage.ContentID
	switch body := native.(type) {
	case responses.Request:
		if request.API != protocol.APIStyleResponses {
			return ProviderRequest{}, nil, protocol.ErrInvalid
		}
		profile, ids, err = storage.StoreResponsesRequest(store, body)
	case anthropic.Request:
		if request.API != protocol.APIStyleAnthropicMessages {
			return ProviderRequest{}, nil, protocol.ErrInvalid
		}
		profile, ids, err = storage.StoreAnthropicRequest(store, body)
	default:
		return ProviderRequest{}, nil, fmt.Errorf("unsupported provider request %T", native)
	}
	if err != nil {
		return ProviderRequest{}, nil, err
	}
	return finishStoredRequest(store, request, profile, ids)
}

// Only the one-time historical migration disassembles an already-serialized request.
func packLegacyRequest(ctx context.Context, tx *sql.Tx, request ProviderRequest, body json.RawMessage) (ProviderRequest, []byte, error) {
	store := newContentStore(ctx, tx)
	var profile storage.ContentID
	var ids []storage.ContentID
	var err error
	switch request.API {
	case protocol.APIStyleResponses:
		profile, ids, err = storage.StoreResponsesRequestJSON(store, body)
	case protocol.APIStyleAnthropicMessages:
		profile, ids, err = storage.StoreAnthropicRequestJSON(store, body)
	default:
		return ProviderRequest{}, nil, protocol.ErrInvalid
	}
	if err != nil {
		return ProviderRequest{}, nil, err
	}
	return finishStoredRequest(store, request, profile, ids)
}

func saveProviderRequest(ctx context.Context, tx *sql.Tx, id, conversationID string, request *ProviderRequest) error {
	if request == nil {
		return nil
	}
	_, err := tx.ExecContext(ctx, `INSERT INTO sampler_model_request(id,conversation_id,context_id,profile_id) VALUES($1,$2,$3,$4)`, id, conversationID, nullableContextID(request.ContextID), request.ProfileID)
	return err
}

// Signed Anthropic continuations depend on unchanged system/tool definitions.
// Freeze those authored fields at the first request rather than rebuilding them after a roll.
func freezeProviderInstructions(ctx context.Context, tx *sql.Tx, conversationID string, native any) (any, error) {
	var profile storage.ContentID
	var previousContext sql.NullString
	err := tx.QueryRowContext(ctx, `SELECT profile_id,context_id FROM sampler_model_request WHERE conversation_id=$1 ORDER BY position DESC LIMIT 1`, conversationID).Scan(&profile, &previousContext)
	if errors.Is(err, sql.ErrNoRows) {
		return native, nil
	}
	if err != nil {
		return nil, err
	}
	store := newContentStore(ctx, tx)
	switch body := native.(type) {
	case responses.Request:
		wire, err := storage.RestoreResponsesRequest(store, profile, nil)
		if err != nil {
			return nil, err
		}
		var previous responses.Request
		if err := json.Unmarshal(wire, &previous); err != nil {
			return nil, err
		}
		body.Instructions = previous.Instructions
		body.Tools = previous.Tools
		// Responses Lite carries its tool definitions in an additional_tools input item.
		if len(body.Input) > 0 {
			if first, ok := body.Input[0].(responses.Item); ok && first.Type == responses.ItemAdditionalTools {
				ids, err := store.loadContext(previousContext.String)
				if err != nil {
					return nil, err
				}
				if len(ids) > 0 {
					records, err := store.restoreRecords(protocol.APIStyleResponses, ids[:1])
					if err != nil {
						return nil, err
					}
					var frozen responses.Item
					if err := json.Unmarshal(records[0], &frozen); err != nil {
						return nil, err
					}
					if frozen.Type == responses.ItemAdditionalTools {
						body.Input = append([]responses.RequestInput(nil), body.Input...)
						body.Input[0] = frozen
					}
				}
			}
		}
		return body, nil
	case anthropic.Request:
		wire, err := storage.RestoreAnthropicRequest(store, profile, nil)
		if err != nil {
			return nil, err
		}
		var previous anthropic.Request
		if err := json.Unmarshal(wire, &previous); err != nil {
			return nil, err
		}
		body.System = previous.System
		body.Tools = previous.Tools
		return body, nil
	default:
		return nil, protocol.ErrInvalid
	}
}

type StoredProviderRequest struct {
	RequestID string
	ContextID string
	ProfileID storage.ContentID
	Method    string
	URL       string
	Headers   map[string]string
	Body      []byte
}

// Authorization is rooted in Session ownership; content hashes are never public read handles.
func (s *Store) LoadProviderRequest(ctx context.Context, sessionID, accountUID, requestID string) (*StoredProviderRequest, error) {
	if _, err := uuid.Parse(requestID); err != nil {
		return nil, protocol.ErrMissing
	}
	var result *StoredProviderRequest
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		var raw []byte
		if err := tx.QueryRowContext(ctx, `SELECT i.request FROM sampler_intent i JOIN sampler_session s ON s.conversation_id=i.conversation_id
            WHERE s.id=$1 AND s.account_uid=$2 AND i.id=$3`, sessionID, accountUID, requestID).Scan(&raw); err != nil {
			if errors.Is(err, sql.ErrNoRows) {
				return protocol.ErrMissing
			}
			return err
		}
		var intent IntentRequest
		if err := json.Unmarshal(raw, &intent); err != nil {
			return err
		}
		if intent.ProviderRequest == nil || intent.ProfileID == "" {
			return protocol.ErrMissing
		}
		body, err := renderProviderRequest(newContentStore(ctx, tx), *intent.ProviderRequest)
		if err != nil {
			return err
		}
		result = &StoredProviderRequest{RequestID: requestID, ContextID: intent.ContextID, ProfileID: intent.ProfileID, Method: intent.Method, URL: intent.URL, Headers: intent.Headers, Body: body}
		return nil
	})
	return result, err
}
