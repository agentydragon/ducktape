package sessionstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore/internal/contentdb"
	"github.com/lib/pq"
)

func (conversation *Conversation) projectHistory(ctx context.Context, tx *sql.Tx, store *contentdb.Store, key providerrequest.ProjectionKey, project func(int) ([]json.RawMessage, error)) (string, string, error) {
	native := conversation.Context()
	source := conversation.storedContexts[native]
	if source.Count != native.RecordCount() {
		return "", "", fmt.Errorf("request source context has uncommitted records")
	}
	var previousSource, previousView sql.NullString
	err := tx.QueryRowContext(ctx, `SELECT source_context_id,context_id FROM sampler_model_request
        WHERE conversation_id=$1 AND projection_key=$2 AND source_context_id IS NOT NULL ORDER BY position DESC LIMIT 1`, conversation.ID, key).Scan(&previousSource, &previousView)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return "", "", err
	}
	start := 0
	head := ""
	if previousSource.Valid {
		extends, err := store.ContextExtends(source.Head, previousSource.String)
		if err != nil {
			return "", "", err
		}
		if extends {
			if start, err = store.ContextItemCount(previousSource.String); err != nil {
				return "", "", err
			}
			head = previousView.String
			var projectedCount int
			if projectedCount, err = store.ContextItemCount(head); err != nil {
				return "", "", err
			}
			if projectedCount != start {
				return "", "", fmt.Errorf("request projection lost source record alignment")
			}
		}
	}
	records, err := project(start)
	if err != nil {
		return "", "", err
	}
	if start+len(records) != source.Count {
		return "", "", fmt.Errorf("request projection changed native record cardinality")
	}
	ids := make([]content.ContentID, 0, len(records))
	for _, record := range records {
		id, err := providerrequest.StoreRecord(store, native.API(), record)
		if err != nil {
			return "", "", err
		}
		ids = append(ids, id)
	}
	head, err = store.AppendContextIDs(native.API(), head, start, ids)
	return source.Head, head, err
}

func previousRequest(ctx context.Context, tx *sql.Tx, conversationID string, kind PendingIntentKind) (ProviderRequest, error) {
	var request ProviderRequest
	var context sql.NullString
	var prefix pq.StringArray
	err := tx.QueryRowContext(ctx, `SELECT mr.profile_id,mr.context_id,mr.prefix_ids FROM sampler_model_request mr
        JOIN sampler_intent i ON i.id=mr.id
        WHERE mr.conversation_id=$1 AND ($2='' OR i.request->>'kind'=$2)
        ORDER BY mr.position DESC LIMIT 1`, conversationID, string(kind)).Scan(&request.ProfileID, &context, &prefix)
	if errors.Is(err, sql.ErrNoRows) {
		return ProviderRequest{}, nil
	}
	for _, id := range prefix {
		request.PrefixIDs = append(request.PrefixIDs, content.ContentID(id))
	}
	request.ContextID = context.String
	return request, err
}
