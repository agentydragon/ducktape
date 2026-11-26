package sessionstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore/internal/contentdb"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

// requestPreparation is private transaction-local plumbing. Public Store methods accept the
// typed providerrequest.Preparation recipe and build this operation inside the transaction.
type requestPreparation func(context.Context, *sql.Tx, *contentdb.Store, *Conversation, ProviderRequest) (ProviderRequest, error)

func renderProviderRequest(store *contentdb.Store, request ProviderRequest) ([]byte, error) {
	ids, err := store.LoadContext(request.ContextID)
	if err != nil {
		return nil, err
	}
	ids = append(append(append([]content.ContentID{}, request.PrefixIDs...), ids...), request.SuffixIDs...)
	roots := append([]content.ContentID{request.ProfileID}, ids...)
	if err := store.Prefetch(roots); err != nil {
		return nil, err
	}
	return providerrequest.Render(store, request.API, request.ProfileID, ids)
}

// renderStartedProviderRequest uses the recipe committed by the begin transaction. On failure the
// intent remains durable for normal provider recovery. The caller has not sent a request yet.
func (s *Store) renderStartedProviderRequest(ctx context.Context, owner Owner, id string, request *ProviderRequest) (string, []byte, error) {
	if request == nil {
		return id, nil, nil
	}
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	var body []byte
	err := postgres.ReadTransaction(ctx, s.db, sql.TxOptions{ReadOnly: true}, func(tx *sql.Tx) error {
		var err error
		body, err = renderProviderRequest(newContentDB(ctx, tx), *request)
		if err != nil {
			return err
		}
		// Rendering can take time for long contexts. Check the attempt and owner again from
		// the current committed state before returning the bytes for dispatch.
		var ready bool
		err = tx.QueryRowContext(ctx, `SELECT EXISTS(
			SELECT 1 FROM sampler_conversation c
			JOIN sampler_intent i ON i.conversation_id=c.id AND i.generation=c.generation
			WHERE c.id=$1 AND c.owner=$2 AND c.generation=$3 AND c.lease_until>clock_timestamp()
				AND c.pending->>'id'=i.id::text AND c.pending->>'kind'=i.request->>'kind'
				AND i.id=$4 AND i.result IS NULL AND i.provider_attempt_lease_until>clock_timestamp()
		)`, owner.ConversationID, owner.Token, owner.Generation, id).Scan(&ready)
		if err != nil {
			return err
		}
		if !ready {
			return protocol.ErrPrecondition
		}
		return checkInterrupt(ctx, tx, owner)
	})
	if err != nil {
		return id, nil, fmt.Errorf("render committed provider request %s: %w", id, err)
	}
	return id, body, nil
}

func finishStoredRequest(store *contentdb.Store, request ProviderRequest, profile content.ContentID, ids []content.ContentID) (ProviderRequest, error) {
	contextID, err := store.StoreContext(request.API, ids)
	if err != nil {
		return ProviderRequest{}, err
	}
	request.ContextID = contextID
	request.ProfileID = profile
	return request, nil
}

func saveProviderRequest(ctx context.Context, tx *sql.Tx, id, conversationID string, request *ProviderRequest) error {
	if request == nil {
		return nil
	}
	_, err := tx.ExecContext(ctx, `INSERT INTO sampler_model_request(id,conversation_id,context_id,profile_id,source_context_id,projection_key,prefix_ids,suffix_ids) VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, id, conversationID, nullableContextID(request.ContextID), request.ProfileID,
		nullableContextID(request.SourceContextID), request.ProjectionKey, contentIDArray(request.PrefixIDs), contentIDArray(request.SuffixIDs))
	return err
}

func nullableContextID(id string) any {
	if id == "" {
		return nil
	}
	return id
}

type StoredProviderRequest struct {
	RequestID string
	ContextID string
	ProfileID content.ContentID
	Method    string
	URL       string
	Headers   map[string]string
	Body      []byte
}

// Authorization is rooted in Session ownership; content hashes are never public read handles.
func (s *Store) LoadProviderRequest(ctx context.Context, sessionID, ownerKey, requestID string) (*StoredProviderRequest, error) {
	if _, err := uuid.Parse(requestID); err != nil {
		return nil, protocol.ErrMissing
	}
	var result *StoredProviderRequest
	err := s.transaction(ctx, "sampler.load_provider_request", func(ctx context.Context, tx *sql.Tx) error {
		var raw []byte
		if err := tx.QueryRowContext(ctx, `SELECT i.request FROM sampler_intent i JOIN sampler_session s ON s.conversation_id=i.conversation_id
            WHERE s.id=$1 AND COALESCE(s.service_spiffe_id,s.account_uid)=$2 AND i.id=$3`, sessionID, ownerKey, requestID).Scan(&raw); err != nil {
			if errors.Is(err, sql.ErrNoRows) {
				return protocol.ErrMissing
			}
			return err
		}
		var intent IntentPayload
		if err := json.Unmarshal(raw, &intent); err != nil {
			return err
		}
		if intent.ProviderRequest == nil || intent.ProfileID == "" {
			return protocol.ErrMissing
		}
		body, err := renderProviderRequest(newContentDB(ctx, tx), *intent.ProviderRequest)
		if err != nil {
			return err
		}
		result = &StoredProviderRequest{RequestID: requestID, ContextID: intent.ContextID, ProfileID: intent.ProfileID, Method: intent.Method, URL: intent.URL, Headers: intent.Headers, Body: body}
		return nil
	})
	return result, err
}

func contentIDArray(ids []content.ContentID) pq.StringArray {
	values := make(pq.StringArray, 0, len(ids))
	for _, id := range ids {
		values = append(values, string(id))
	}
	return values
}
