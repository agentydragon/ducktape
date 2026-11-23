package contentdb

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/lib/pq"
)

// ContentReferences resolves the provider-owned children of a stored content row.
type ContentReferences func(content.ContentKind, json.RawMessage) ([]content.ContentID, error)

// Store provides transaction-scoped access to content and immutable context chains.
// The caller owns tx and decides whether the work commits or rolls back.
type Store struct {
	ctx        context.Context
	tx         *sql.Tx
	references ContentReferences
	cache      map[content.ContentID]content.StoredContent
	pending    map[content.ContentID]content.StoredContent
}

var _ providerrequest.Content = (*Store)(nil)

func New(ctx context.Context, tx *sql.Tx, references ContentReferences) *Store {
	return &Store{
		ctx: ctx, tx: tx, references: references,
		cache:   map[content.ContentID]content.StoredContent{},
		pending: map[content.ContentID]content.StoredContent{},
	}
}

func (s *Store) PutContent(kind content.ContentKind, payload json.RawMessage) (content.ContentID, error) {
	stored, err := content.CanonicalContent(kind, payload)
	if err != nil {
		return "", err
	}
	s.cache[stored.ID] = stored
	s.pending[stored.ID] = stored
	return stored.ID, nil
}

func (s *Store) GetContent(id content.ContentID) (content.StoredContent, error) {
	if stored, ok := s.cache[id]; ok {
		return stored, nil
	}
	stored := content.StoredContent{ID: id}
	if err := s.tx.QueryRowContext(s.ctx, `SELECT kind,payload FROM sampler_content WHERE id=$1`, id).Scan(&stored.Kind, &stored.Payload); err != nil {
		return stored, err
	}
	if err := content.VerifyContent(stored); err != nil {
		return stored, err
	}
	s.cache[id] = stored
	return stored, nil
}

// Prefetch resolves each breadth in batches so reading a long context does not
// make a database roundtrip per block.
func (s *Store) Prefetch(ids []content.ContentID) error {
	seen := map[content.ContentID]bool{}
	for len(ids) > 0 {
		level := make([]content.ContentID, 0, len(ids))
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
				var stored content.StoredContent
				if err := rows.Scan(&stored.ID, &stored.Kind, &stored.Payload); err != nil {
					rows.Close()
					return err
				}
				if err := content.VerifyContent(stored); err != nil {
					rows.Close()
					return err
				}
				s.cache[stored.ID] = stored
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
			stored, ok := s.cache[id]
			if !ok {
				return fmt.Errorf("missing provider content %s", id)
			}
			refs, err := s.references(stored.Kind, stored.Payload)
			if err != nil {
				return err
			}
			ids = append(ids, refs...)
		}
	}
	return nil
}

func (s *Store) Save() error {
	ids := make([]content.ContentID, 0, len(s.pending))
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
		known := map[content.ContentID]bool{}
		for rows.Next() {
			var id content.ContentID
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
			stored := s.pending[id]
			keys = append(keys, string(id))
			kinds = append(kinds, string(stored.Kind))
			payloads = append(payloads, stored.Payload)
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
	API     protocol.APIStyle `json:"api"`
	Parent  string            `json:"parent"`
	Content content.ContentID `json:"content"`
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

func (s *Store) StoreContext(api protocol.APIStyle, contents []content.ContentID) (string, error) {
	if api != protocol.APIStyleResponses && api != protocol.APIStyleAnthropicMessages && api != protocol.APIStyleChatCompletions {
		return "", protocol.ErrInvalid
	}
	if err := s.Save(); err != nil {
		return "", err
	}
	items := make([]contextItem, len(contents))
	ids := make([]string, len(contents))
	parent := ""
	for i, contentID := range contents {
		items[i] = contextItem{api, parent, contentID}
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

// AppendContextIDs stores only newly appended records, sharing the immutable prefix.
func (s *Store) AppendContextIDs(api protocol.APIStyle, root string, prefixCount int, suffix []content.ContentID) (string, error) {
	if prefixCount > 0 && root == "" {
		return "", errors.New("nonempty provider context prefix has no root")
	}
	if prefixCount == 0 && root != "" {
		return "", errors.New("empty provider context prefix has a root")
	}
	if err := s.Save(); err != nil {
		return "", err
	}
	parent := root
	for index, contentID := range suffix {
		item := contextItem{API: api, Parent: parent, Content: contentID}
		id := contextItemID(item)
		if _, err := s.tx.ExecContext(s.ctx, `INSERT INTO sampler_context(id,api,parent_id,content_id,item_count)
			VALUES($1,$2,$3,$4,$5) ON CONFLICT(id) DO NOTHING`, id, api, nullableContextID(parent), contentID, prefixCount+index+1); err != nil {
			return "", err
		}
		parent = id
	}
	return parent, nil
}

func (s *Store) LoadContext(id string) ([]content.ContentID, error) {
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
	var ids []content.ContentID
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

func (s *Store) ContextExtends(current, base string) (bool, error) {
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

func (s *Store) ContextItemCount(id string) (int, error) {
	var count int
	err := s.tx.QueryRowContext(s.ctx, `SELECT item_count FROM sampler_context WHERE id=$1`, id).Scan(&count)
	return count, err
}
