package centralsampler

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
	"github.com/lib/pq"
)

// contentStore interns only the provider records defined in protocol.ContentKind.
// Pending rows are saved together in deterministic order before publishing any roots.
type contentStore struct {
	ctx     context.Context
	tx      *sql.Tx
	cache   map[protocol.ContentID]protocol.StoredContent
	pending map[protocol.ContentID]protocol.StoredContent
}

func newContentStore(ctx context.Context, tx *sql.Tx) *contentStore {
	return &contentStore{ctx: ctx, tx: tx, cache: map[protocol.ContentID]protocol.StoredContent{}, pending: map[protocol.ContentID]protocol.StoredContent{}}
}

func (s *contentStore) PutContent(kind protocol.ContentKind, payload json.RawMessage) (protocol.ContentID, error) {
	content, err := protocol.CanonicalContent(kind, payload)
	if err != nil {
		return "", err
	}
	s.cache[content.ID] = content
	s.pending[content.ID] = content
	return content.ID, nil
}

func (s *contentStore) GetContent(id protocol.ContentID) (protocol.StoredContent, error) {
	if content, ok := s.cache[id]; ok {
		return content, nil
	}
	content := protocol.StoredContent{ID: id}
	if err := s.tx.QueryRowContext(s.ctx, `SELECT kind,payload FROM sampler_content WHERE id=$1`, id).Scan(&content.Kind, &content.Payload); err != nil {
		return content, err
	}
	if err := protocol.VerifyContent(content); err != nil {
		return content, err
	}
	s.cache[id] = content
	return content, nil
}

// The provider envelopes have a bounded nesting depth. Resolve each breadth in
// batches so reading a long context does not make a database roundtrip per block.
func (s *contentStore) prefetch(ids []protocol.ContentID) error {
	seen := map[protocol.ContentID]bool{}
	for len(ids) > 0 {
		level := make([]protocol.ContentID, 0, len(ids))
		missing := []string{}
		for _, id := range ids {
			if id == "" || seen[id] {
				continue
			}
			seen[id] = true
			level = append(level, id)
			if _, ok := s.cache[id]; !ok {
				missing = append(missing, string(id))
			}
		}
		for len(missing) > 0 {
			n := min(len(missing), 256)
			rows, err := s.tx.QueryContext(s.ctx, `SELECT id,kind,payload FROM sampler_content WHERE id=ANY($1::text[])`, pq.Array(missing[:n]))
			if err != nil {
				return err
			}
			for rows.Next() {
				var content protocol.StoredContent
				if err := rows.Scan(&content.ID, &content.Kind, &content.Payload); err != nil {
					rows.Close()
					return err
				}
				if err := protocol.VerifyContent(content); err != nil {
					rows.Close()
					return err
				}
				s.cache[content.ID] = content
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				return err
			}
			missing = missing[n:]
		}
		ids = nil
		for _, id := range level {
			content, ok := s.cache[id]
			if !ok {
				return fmt.Errorf("missing provider content %s", id)
			}
			var refs []protocol.ContentID
			var err error
			switch content.Kind {
			case protocol.ContentOpenAIItem, protocol.ContentOpenAIContent, protocol.ContentOpenAIRequest:
				refs, err = responses.ContentReferences(content.Kind, content.Payload)
			case protocol.ContentAnthropicMessage, protocol.ContentAnthropicText, protocol.ContentAnthropicThinking,
				protocol.ContentAnthropicRedactedThinking, protocol.ContentAnthropicToolUse, protocol.ContentAnthropicToolResult,
				protocol.ContentAnthropicImage, protocol.ContentAnthropicCompaction, protocol.ContentAnthropicRequest:
				refs, err = anthropic.ContentReferences(content.Kind, content.Payload)
			}
			if err != nil {
				return err
			}
			ids = append(ids, refs...)
		}
	}
	return nil
}

func (s *contentStore) save() error {
	ids := make([]protocol.ContentID, 0, len(s.pending))
	for id := range s.pending {
		ids = append(ids, id)
	}
	slices.Sort(ids)
	// Bound parameters and transaction memory even for a large image-heavy context.
	for len(ids) > 0 {
		n := min(len(ids), 256)
		batch := ids[:n]
		ids = ids[n:]
		candidates := make([]string, 0, n)
		for _, id := range batch {
			candidates = append(candidates, string(id))
		}
		rows, err := s.tx.QueryContext(s.ctx, `SELECT id FROM sampler_content WHERE id=ANY($1::text[])`, pq.Array(candidates))
		if err != nil {
			return err
		}
		known := map[protocol.ContentID]bool{}
		for rows.Next() {
			var id protocol.ContentID
			if err := rows.Scan(&id); err != nil {
				rows.Close()
				return err
			}
			known[id] = true
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		keys := make([]string, 0, n)
		kinds := make([]string, 0, n)
		payloads := make([][]byte, 0, n)
		for _, id := range batch {
			if known[id] {
				continue
			}
			content := s.pending[id]
			keys = append(keys, string(id))
			kinds = append(kinds, string(content.Kind))
			payloads = append(payloads, content.Payload)
		}
		if len(keys) == 0 {
			continue
		}
		if _, err := s.tx.ExecContext(s.ctx, `INSERT INTO sampler_content(id,kind,payload)
            SELECT * FROM unnest($1::text[],$2::text[],$3::bytea[]) ON CONFLICT(id) DO NOTHING`, pq.Array(keys), pq.Array(kinds), pq.ByteaArray(payloads)); err != nil {
			return err
		}
	}
	clear(s.pending)
	return nil
}

type contextItem struct {
	API     protocol.APIStyle  `json:"api"`
	Parent  string             `json:"parent"`
	Content protocol.ContentID `json:"content"`
}

func contextItemID(item contextItem) string {
	// This is a fixed sampler context record, encoded by the standard JSON codec.
	wire, _ := json.Marshal(item)
	sum := sha256.Sum256(append([]byte("agentplane/provider-context/v1\x00"), wire...))
	return hex.EncodeToString(sum[:])
}

func nullableContextID(id string) any {
	if id == "" {
		return nil
	}
	return id
}

func (s *contentStore) storeContext(api protocol.APIStyle, contents []protocol.ContentID) (string, error) {
	if api != protocol.APIStyleResponses && api != protocol.APIStyleAnthropicMessages {
		return "", protocol.ErrInvalid
	}
	if err := s.save(); err != nil {
		return "", err
	}
	items := make([]contextItem, len(contents))
	ids := make([]string, len(contents))
	parent := ""
	for i, content := range contents {
		items[i] = contextItem{api, parent, content}
		ids[i] = contextItemID(items[i])
		parent = ids[i]
	}
	if parent == "" {
		return "", nil
	}
	// Most writes leave the context unchanged. A single lookup avoids rewriting its prefix.
	var exists bool
	if err := s.tx.QueryRowContext(s.ctx, `SELECT EXISTS(SELECT 1 FROM sampler_context WHERE id=$1)`, parent).Scan(&exists); err != nil {
		return "", err
	}
	if exists {
		return parent, nil
	}
	known := map[string]bool{}
	rows, err := s.tx.QueryContext(s.ctx, `SELECT id FROM sampler_context WHERE id=ANY($1::text[])`, pq.Array(ids))
	if err != nil {
		return "", err
	}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return "", err
		}
		known[id] = true
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return "", err
	}
	keys := []string{}
	parents := []string{}
	contentIDs := []string{}
	counts := []int64{}
	for i, item := range items {
		if known[ids[i]] {
			continue
		}
		keys = append(keys, ids[i])
		parents = append(parents, item.Parent)
		contentIDs = append(contentIDs, string(item.Content))
		counts = append(counts, int64(i+1))
	}
	for len(keys) > 0 {
		n := min(len(keys), 256)
		if _, err := s.tx.ExecContext(s.ctx, `INSERT INTO sampler_context(id,api,parent_id,content_id,item_count)
            SELECT id,$1,NULLIF(parent,''),content,cnt FROM unnest($2::text[],$3::text[],$4::text[],$5::bigint[]) AS t(id,parent,content,cnt)
            ON CONFLICT(id) DO NOTHING`, api, pq.Array(keys[:n]), pq.Array(parents[:n]), pq.Array(contentIDs[:n]), pq.Array(counts[:n])); err != nil {
			return "", err
		}
		keys = keys[n:]
		parents = parents[n:]
		contentIDs = contentIDs[n:]
		counts = counts[n:]
	}
	return parent, nil
}

func (s *contentStore) checkTranscriptPrefix(previous, current string) error {
	if previous == "" || previous == current {
		return nil
	}
	var retained bool
	if err := s.tx.QueryRowContext(s.ctx, `WITH RECURSIVE prefix AS (
        SELECT id,parent_id,item_count FROM sampler_context WHERE id=$1
        UNION ALL SELECT c.id,c.parent_id,c.item_count FROM sampler_context c JOIN prefix p ON c.id=p.parent_id AND c.item_count=p.item_count-1
    ) SELECT EXISTS(SELECT 1 FROM prefix WHERE id=$2)`, current, previous).Scan(&retained); err != nil {
		return err
	}
	if !retained {
		return fmt.Errorf("retained conversation transcript is not an append-only prefix")
	}
	return nil
}

func (s *contentStore) loadContext(id string) ([]protocol.ContentID, error) {
	if id == "" {
		return nil, nil
	}
	rows, err := s.tx.QueryContext(s.ctx, `WITH RECURSIVE prefix AS (
        SELECT id,api,parent_id,content_id,item_count FROM sampler_context WHERE id=$1
        UNION ALL SELECT c.id,c.api,c.parent_id,c.content_id,c.item_count FROM sampler_context c JOIN prefix p ON c.id=p.parent_id AND c.item_count=p.item_count-1 AND c.api=p.api
    ) SELECT id,api,COALESCE(parent_id,''),content_id,item_count FROM prefix ORDER BY item_count`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []protocol.ContentID
	previous := ""
	for rows.Next() {
		var key string
		var item contextItem
		var count int
		if err := rows.Scan(&key, &item.API, &item.Parent, &item.Content, &count); err != nil {
			return nil, err
		}
		if count != len(ids)+1 || item.Parent != previous || contextItemID(item) != key {
			return nil, fmt.Errorf("invalid provider context %s", key)
		}
		previous = key
		ids = append(ids, item.Content)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if previous != id {
		return nil, fmt.Errorf("missing provider context %s", id)
	}
	return ids, nil
}

func (s *contentStore) storeRecord(api protocol.APIStyle, record json.RawMessage) (protocol.ContentID, error) {
	switch api {
	case protocol.APIStyleResponses:
		return responses.StoreRecord(s, record)
	case protocol.APIStyleAnthropicMessages:
		return anthropic.StoreRecord(s, record)
	default:
		return "", protocol.ErrInvalid
	}
}

func (s *contentStore) restoreRecords(api protocol.APIStyle, ids []protocol.ContentID) ([]json.RawMessage, error) {
	if err := s.prefetch(ids); err != nil {
		return nil, err
	}
	records := make([]json.RawMessage, 0, len(ids))
	for _, id := range ids {
		var record json.RawMessage
		var err error
		switch api {
		case protocol.APIStyleResponses:
			record, err = responses.RestoreRecord(s, id)
		case protocol.APIStyleAnthropicMessages:
			record, err = anthropic.RestoreRecord(s, id)
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

func (s *contentStore) storeNativeContext(native protocol.Context) (string, error) {
	records, err := native.Records()
	if err != nil {
		return "", err
	}
	ids := make([]protocol.ContentID, 0, len(records))
	for _, record := range records {
		id, err := s.storeRecord(native.API(), record)
		if err != nil {
			return "", err
		}
		ids = append(ids, id)
	}
	return s.storeContext(native.API(), ids)
}

type nativeControl struct {
	PendingEffort *string `json:"pending_effort,omitempty"`
}

func nativeContextControl(native protocol.Context) (json.RawMessage, error) {
	control := nativeControl{}
	if c, ok := native.(*anthropic.Context); ok {
		control.PendingEffort = c.PendingEffort
	}
	return json.Marshal(control)
}

func (s *contentStore) loadNativeContext(api protocol.APIStyle, id string, control json.RawMessage) (protocol.Context, error) {
	ids, err := s.loadContext(id)
	if err != nil {
		return nil, err
	}
	records, err := s.restoreRecords(api, ids)
	if err != nil {
		return nil, err
	}
	native, err := decodeNativeRecords(api, records)
	if err != nil {
		return nil, err
	}
	if c, ok := native.(*anthropic.Context); ok && len(control) > 0 {
		var state nativeControl
		if err := json.Unmarshal(control, &state); err != nil {
			return nil, err
		}
		c.PendingEffort = state.PendingEffort
	}
	return native, nil
}
