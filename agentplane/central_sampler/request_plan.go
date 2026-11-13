package centralsampler

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/storage"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
	"github.com/lib/pq"
)

// A request view shares its history independently of request-specific prefixes
// and suffixes. The key versions the provider's native projection semantics.
type requestProjectionKey string

const (
	anthropicProjectionKey     requestProjectionKey = "anthropic_messages/v1"
	responsesProjectionVersion                      = "responses/v1/"
)

func (s *contentStore) projectHistory(conversation *Conversation, key requestProjectionKey, project func(int) ([]json.RawMessage, error)) (string, string, error) {
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
	ids := make([]storage.ContentID, 0, len(records))
	for _, record := range records {
		id, err := s.storeRecord(native.API(), record)
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
		request.PrefixIDs = append(request.PrefixIDs, storage.ContentID(id))
	}
	request.ContextID = context.String
	return request, err
}

func prepareResponsesRequest(s *contentStore, conversation *Conversation, metadata ProviderRequest, body responses.Request, config responses.Config, encoding protocol.ExecEncoding, compact bool) (ProviderRequest, []byte, error) {
	previous, err := s.previousRequest(conversation.ID)
	if err != nil {
		return ProviderRequest{}, nil, err
	}
	if previous.ProfileID != "" {
		body, err = responses.FreezeRequest(s, previous.ProfileID, body)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		refs := previous.PrefixIDs
		if len(refs) == 0 {
			refs, err = s.loadContext(previous.ContextID)
			if err != nil {
				return ProviderRequest{}, nil, err
			}
		}
		if len(refs) > 0 {
			body, err = responses.FreezeToolDeclaration(s, refs[0], body)
			if err != nil {
				return ProviderRequest{}, nil, err
			}
		}
	}
	if compact {
		if len(body.Input) == 0 {
			return ProviderRequest{}, nil, protocol.ErrInvalid
		}
		raw, err := json.Marshal(body.Input[len(body.Input)-1])
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		id, err := responses.StoreRecord(s, raw)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		metadata.SuffixIDs = []storage.ContentID{id}
		body.Input = body.Input[:len(body.Input)-1]
	}
	for _, input := range body.Input {
		raw, err := json.Marshal(input)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		id, err := responses.StoreRecord(s, raw)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
		metadata.PrefixIDs = append(metadata.PrefixIDs, id)
	}
	body.Input = nil
	metadata.ProfileID, _, err = responses.StoreRequest(s, body)
	if err != nil {
		return ProviderRequest{}, nil, err
	}
	metadata.ProjectionKey = requestProjectionKey(responsesProjectionVersion + string(config.Dialect) + "/" + string(encoding))
	source := conversation.Context().(*responses.Context)
	metadata.SourceContextID, metadata.ContextID, err = s.projectHistory(conversation, metadata.ProjectionKey, func(start int) ([]json.RawMessage, error) { return source.ProjectRecords(start, config, encoding) })
	if err != nil {
		return ProviderRequest{}, nil, err
	}
	if err := s.save(); err != nil {
		return ProviderRequest{}, nil, err
	}
	wire, err := renderProviderRequest(s, metadata)
	return metadata, wire, err
}

func prepareAnthropicRequest(s *contentStore, conversation *Conversation, metadata ProviderRequest, body anthropic.Request) (ProviderRequest, []byte, error) {
	previous, err := s.previousRequest(conversation.ID)
	if err != nil {
		return ProviderRequest{}, nil, err
	}
	if previous.ProfileID != "" {
		body, err = anthropic.FreezeRequest(s, previous.ProfileID, body)
		if err != nil {
			return ProviderRequest{}, nil, err
		}
	}
	body.Messages = []json.RawMessage{}
	metadata.ProfileID, _, err = anthropic.StoreRequest(s, body)
	if err != nil {
		return ProviderRequest{}, nil, err
	}
	metadata.ProjectionKey = anthropicProjectionKey
	source := conversation.Context().(*anthropic.Context)
	metadata.SourceContextID, metadata.ContextID, err = s.projectHistory(conversation, metadata.ProjectionKey, source.ProjectRecords)
	if err != nil {
		return ProviderRequest{}, nil, err
	}
	if err := s.save(); err != nil {
		return ProviderRequest{}, nil, err
	}
	wire, err := renderProviderRequest(s, metadata)
	return metadata, wire, err
}
