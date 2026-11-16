package sessionstore

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/providerrequest"
	"github.com/lib/pq"
)

func (s *contentStore) projectHistory(conversation *Conversation, key providerrequest.ProjectionKey, project func(int) ([]json.RawMessage, error)) (string, string, error) {
	native := conversation.Context()
	source := conversation.storedContexts[native]
	if source.Count != native.RecordCount() {
		return "", "", fmt.Errorf("request source context has uncommitted records")
	}
	var previousSource, previousView sql.NullString
	err := s.tx.QueryRowContext(s.ctx, `SELECT source_context_id,context_id FROM sampler_model_request
        WHERE conversation_id=$1 AND projection_key=$2 AND source_context_id IS NOT NULL ORDER BY position DESC LIMIT 1`, conversation.ID, key).Scan(&previousSource, &previousView)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return "", "", err
	}
	start := 0
	head := ""
	if previousSource.Valid {
		extends, err := s.contextExtends(source.Head, previousSource.String)
		if err != nil {
			return "", "", err
		}
		if extends {
			if err := s.tx.QueryRowContext(s.ctx, `SELECT item_count FROM sampler_context WHERE id=$1`, previousSource.String).Scan(&start); err != nil {
				return "", "", err
			}
			head = previousView.String
			var projectedCount int
			if err := s.tx.QueryRowContext(s.ctx, `SELECT item_count FROM sampler_context WHERE id=$1`, head).Scan(&projectedCount); err != nil {
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
		id, err := providerrequest.StoreRecord(s, native.API(), record)
		if err != nil {
			return "", "", err
		}
		ids = append(ids, id)
	}
	head, err = appendContextIDs(s, native.API(), head, start, ids)
	return source.Head, head, err
}

func (s *contentStore) contextExtends(current, base string) (bool, error) {
	if current == base {
		return true, nil
	}
	if current == "" {
		return false, nil
	}
	var found bool
	err := s.tx.QueryRowContext(s.ctx, `WITH RECURSIVE chain AS (
        SELECT id,parent_id FROM sampler_context WHERE id=$1
        UNION ALL SELECT p.id,p.parent_id FROM sampler_context p JOIN chain c ON p.id=c.parent_id WHERE c.id<>$2
    ) SELECT EXISTS(SELECT 1 FROM chain WHERE id=$2)`, current, base).Scan(&found)
	return found, err
}

func (s *contentStore) previousRequest(conversationID string) (ProviderRequest, error) {
	var request ProviderRequest
	var context sql.NullString
	var prefix pq.StringArray
	err := s.tx.QueryRowContext(s.ctx, `SELECT profile_id,context_id,prefix_ids FROM sampler_model_request
        WHERE conversation_id=$1 ORDER BY position DESC LIMIT 1`, conversationID).Scan(&request.ProfileID, &context, &prefix)
	if errors.Is(err, sql.ErrNoRows) {
		return ProviderRequest{}, nil
	}
	for _, id := range prefix {
		request.PrefixIDs = append(request.PrefixIDs, content.ContentID(id))
	}
	request.ContextID = context.String
	return request, err
}
