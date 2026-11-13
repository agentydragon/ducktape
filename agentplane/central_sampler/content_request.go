package centralsampler

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/storage"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

func renderProviderRequest(store *contentStore, request ProviderRequest) ([]byte, error) {
	ids, err := store.loadContext(request.ContextID)
	if err != nil {
		return nil, err
	}
	ids = append(append(append([]storage.ContentID{}, request.PrefixIDs...), ids...), request.SuffixIDs...)
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
		return responses.RestoreRequest(store, request.ProfileID, records)
	case protocol.APIStyleAnthropicMessages:
		return anthropic.RestoreRequest(store, request.ProfileID, records)
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
type requestPreparation func(*contentStore, *Conversation, ProviderRequest) (ProviderRequest, []byte, error)

func saveProviderRequest(ctx context.Context, tx *sql.Tx, id, conversationID string, request *ProviderRequest) error {
	if request == nil {
		return nil
	}
	_, err := tx.ExecContext(ctx, `INSERT INTO sampler_model_request(id,conversation_id,context_id,profile_id,source_context_id,projection_key,prefix_ids,suffix_ids) VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, id, conversationID, nullableContextID(request.ContextID), request.ProfileID,
		nullableContextID(request.SourceContextID), request.ProjectionKey, contentIDArray(request.PrefixIDs), contentIDArray(request.SuffixIDs))
	return err
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

func contentIDArray(ids []storage.ContentID) pq.StringArray {
	values := make(pq.StringArray, 0, len(ids))
	for _, id := range ids {
		values = append(values, string(id))
	}
	return values
}
