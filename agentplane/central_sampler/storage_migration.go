package centralsampler

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math/big"
	"reflect"
	"sort"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/storage"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
)

const (
	legacyDispatchScanPageSize = 256
	contentMigrationBatchSize  = 128
	eventMigrationBatchSize    = 256
	intentMigrationBatchSize   = 64
)

// migrateContentStore is the RunDB callback for the single native-content migration. DDL is
// idempotent, while each content conversion commits a bounded suffix and advances a nullable
// root/control marker. If startup is interrupted, the next invocation resumes from those roots.
func migrateContentStore(ctx context.Context, db *sql.DB) error {
	return migrateContentStoreWithBatchSize(ctx, db, contentMigrationBatchSize)
}

func migrateContentStoreWithBatchSize(ctx context.Context, db *sql.DB, batchSize int) error {
	if db == nil || batchSize <= 0 {
		return errors.New("content migration requires a database and positive batch size")
	}
	if _, err := db.ExecContext(ctx, nativeContentSchema); err != nil {
		return fmt.Errorf("create native content schema: %w", err)
	}

	if err := compressLegacyJournal(ctx, db, eventMigrationBatchSize); err != nil {
		return err
	}

	oldItems, err := migrationTableExists(ctx, db, "sampler_conversation_item")
	if err != nil {
		return err
	}
	oldContext, err := migrationColumnExists(ctx, db, "sampler_conversation", "context")
	if err != nil {
		return err
	}
	if oldItems {
		if err := migrateTranscriptContexts(ctx, db, batchSize); err != nil {
			return err
		}
	} else if err := validateCompletedTranscriptRoots(ctx, db); err != nil {
		// The old table can be absent only after validation and retirement committed. This also
		// detects an interrupted or manually edited schema that would otherwise lose transcript data.
		return err
	}
	if oldContext {
		if err := migrateActiveContexts(ctx, db, batchSize); err != nil {
			return err
		}
	}
	if err := migrateProviderRequests(ctx, db, batchSize); err != nil {
		return err
	}
	if err := validateNativeContent(ctx, db, oldItems, oldContext); err != nil {
		return err
	}
	if _, err := db.ExecContext(ctx, retireInlineContentSchema); err != nil {
		return fmt.Errorf("retire inline conversation content: %w", err)
	}
	return nil
}

func migrationTableExists(ctx context.Context, db *sql.DB, table string) (bool, error) {
	var exists bool
	err := db.QueryRowContext(ctx, `SELECT to_regclass(current_schema() || '.' || $1) IS NOT NULL`, table).Scan(&exists)
	return exists, err
}

func migrationColumnExists(ctx context.Context, db *sql.DB, table, column string) (bool, error) {
	var exists bool
	err := db.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM information_schema.columns
		WHERE table_schema=current_schema() AND table_name=$1 AND column_name=$2
	)`, table, column).Scan(&exists)
	return exists, err
}

// compressLegacyJournal changes only the physical representation. The codec inflates entries
// before any caller sees them, preserving the exact event bytes stored by History.
func compressLegacyJournal(ctx context.Context, db *sql.DB, pageSize int) error {
	var afterSession string
	var afterSequence int64
	hasAfter := false
	for {
		tx, err := db.BeginTx(ctx, nil)
		if err != nil {
			return err
		}
		query := `SELECT session_id,sequence,entry FROM sampler_event
			WHERE octet_length(entry)>=4096 AND substring(entry FROM 1 FOR 2)<>decode('1f8b','hex')
			ORDER BY session_id,sequence LIMIT $1`
		args := []any{pageSize}
		if hasAfter {
			query = `SELECT session_id,sequence,entry FROM sampler_event
				WHERE octet_length(entry)>=4096 AND substring(entry FROM 1 FOR 2)<>decode('1f8b','hex')
				AND (session_id,sequence)>($1,$2) ORDER BY session_id,sequence LIMIT $3`
			args = []any{afterSession, afterSequence, pageSize}
		}
		rows, err := tx.QueryContext(ctx, query, args...)
		if err != nil {
			_ = tx.Rollback()
			return fmt.Errorf("scan legacy sampler journal for compression: %w", err)
		}
		type eventRow struct {
			session  string
			sequence int64
			entry    []byte
		}
		page := make([]eventRow, 0, pageSize)
		for rows.Next() {
			var row eventRow
			if err := rows.Scan(&row.session, &row.sequence, &row.entry); err != nil {
				_ = rows.Close()
				_ = tx.Rollback()
				return fmt.Errorf("read legacy sampler journal entry: %w", err)
			}
			page = append(page, row)
		}
		rowsErr := rows.Err()
		closeErr := rows.Close()
		if rowsErr != nil || closeErr != nil {
			_ = tx.Rollback()
			if rowsErr != nil {
				return fmt.Errorf("read legacy sampler journal page: %w", rowsErr)
			}
			return fmt.Errorf("close legacy sampler journal page: %w", closeErr)
		}
		for _, row := range page {
			encoded, err := encodeStoredEvent(row.entry)
			if err != nil {
				_ = tx.Rollback()
				return fmt.Errorf("compress sampler journal %s/%d: %w", row.session, row.sequence, err)
			}
			if !bytes.Equal(encoded, row.entry) {
				if _, err := tx.ExecContext(ctx, `UPDATE sampler_event SET entry=$3 WHERE session_id=$1 AND sequence=$2 AND entry=$4`,
					row.session, row.sequence, encoded, row.entry); err != nil {
					_ = tx.Rollback()
					return fmt.Errorf("store compressed sampler journal %s/%d: %w", row.session, row.sequence, err)
				}
			}
			afterSession, afterSequence, hasAfter = row.session, row.sequence, true
		}
		if err := tx.Commit(); err != nil {
			return fmt.Errorf("commit sampler journal compression batch: %w", err)
		}
		if len(page) == 0 || len(page) < pageSize {
			return nil
		}
	}
}

func migrateTranscriptContexts(ctx context.Context, db *sql.DB, batchSize int) error {
	for {
		var conversationID string
		err := db.QueryRowContext(ctx, `SELECT id::text FROM sampler_conversation
			WHERE transcript_control IS NULL ORDER BY id LIMIT 1`).Scan(&conversationID)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("find conversation with unconverted transcript: %w", err)
		}
		if err := convertTranscriptBatch(ctx, db, conversationID, batchSize); err != nil {
			return err
		}
	}
}

func convertTranscriptBatch(ctx context.Context, db *sql.DB, conversationID string, batchSize int) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	var api protocol.APIStyle
	var oldRoot sql.NullString
	var control []byte
	if err := tx.QueryRowContext(ctx, `SELECT api,transcript_context_id,transcript_control FROM sampler_conversation
		WHERE id=$1 FOR UPDATE`, conversationID).Scan(&api, &oldRoot, &control); err != nil {
		return fmt.Errorf("read transcript root for conversation %s: %w", conversationID, err)
	}
	if control != nil {
		return tx.Commit()
	}
	store := newContentStore(ctx, tx)
	var legacyRecords []json.RawMessage
	prefix, err := store.loadContext(oldRoot.String)
	if err != nil {
		return fmt.Errorf("load transcript prefix for conversation %s: %w", conversationID, err)
	}
	var count, maxOrdinal int64
	if err := tx.QueryRowContext(ctx, `SELECT count(*),COALESCE(max(ordinal),0) FROM sampler_conversation_item WHERE conversation_id=$1`, conversationID).
		Scan(&count, &maxOrdinal); err != nil {
		return fmt.Errorf("count legacy transcript for conversation %s: %w", conversationID, err)
	}
	if int64(len(prefix)) > count || maxOrdinal != count {
		return fmt.Errorf("legacy transcript for conversation %s has %d rows through ordinal %d but converted prefix has %d items", conversationID, count, maxOrdinal, len(prefix))
	}
	rows, err := tx.QueryContext(ctx, `SELECT ordinal,record FROM sampler_conversation_item
		WHERE conversation_id=$1 AND ordinal>$2 ORDER BY ordinal LIMIT $3`, conversationID, len(prefix), batchSize)
	if err != nil {
		return fmt.Errorf("read legacy transcript for conversation %s: %w", conversationID, err)
	}
	var suffix []storage.ContentID
	for rows.Next() {
		var ordinal int64
		var record []byte
		if err := rows.Scan(&ordinal, &record); err != nil {
			_ = rows.Close()
			return fmt.Errorf("read legacy transcript item for conversation %s: %w", conversationID, err)
		}
		want := int64(len(prefix) + len(suffix) + 1)
		if ordinal != want {
			_ = rows.Close()
			return fmt.Errorf("legacy transcript for conversation %s has ordinal %d, expected %d", conversationID, ordinal, want)
		}
		id, err := store.storeRecord(api, record)
		if err != nil {
			_ = rows.Close()
			return fmt.Errorf("convert transcript item %d for conversation %s: %w", ordinal, conversationID, err)
		}
		legacyRecords = append(legacyRecords, append(json.RawMessage(nil), record...))
		suffix = append(suffix, id)
	}
	rowsErr := rows.Err()
	closeErr := rows.Close()
	if rowsErr != nil {
		return fmt.Errorf("read legacy transcript page for conversation %s: %w", conversationID, rowsErr)
	}
	if closeErr != nil {
		return fmt.Errorf("close legacy transcript page for conversation %s: %w", conversationID, closeErr)
	}
	root, err := appendContextIDs(store, api, oldRoot.String, len(prefix), suffix)
	if err != nil {
		return fmt.Errorf("append transcript content for conversation %s: %w", conversationID, err)
	}
	if len(suffix) > 0 {
		storedRecords, err := store.restoreRecords(api, suffix)
		if err != nil {
			return fmt.Errorf("validate converted transcript records for conversation %s: %w", conversationID, err)
		}
		for index, stored := range storedRecords {
			if !jsonEquivalent(legacyRecords[index], stored) {
				return fmt.Errorf("converted transcript item %d changed for conversation %s", len(prefix)+index+1, conversationID)
			}
		}
	}
	converted := len(prefix) + len(suffix)
	if int64(converted) == count {
		if maxOrdinal != int64(converted) {
			return fmt.Errorf("legacy transcript for conversation %s is not a contiguous prefix", conversationID)
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_conversation SET transcript_context_id=$2,transcript_control='{}'::jsonb WHERE id=$1`,
			conversationID, nullableContextID(root)); err != nil {
			return fmt.Errorf("finish transcript conversion for conversation %s: %w", conversationID, err)
		}
	} else {
		if len(suffix) == 0 {
			return fmt.Errorf("legacy transcript for conversation %s is missing ordinal %d", conversationID, converted+1)
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_conversation SET transcript_context_id=$2 WHERE id=$1`,
			conversationID, nullableContextID(root)); err != nil {
			return fmt.Errorf("checkpoint transcript conversion for conversation %s: %w", conversationID, err)
		}
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit transcript conversion for conversation %s: %w", conversationID, err)
	}
	return nil
}

// appendContextIDs inserts only the newly converted suffix. Rehashing the whole prefix in each
// transaction would make a long conversation quadratic in the number of batches.
func appendContextIDs(store *contentStore, api protocol.APIStyle, root string, prefixCount int, suffix []storage.ContentID) (string, error) {
	if prefixCount > 0 && root == "" {
		return "", errors.New("nonempty provider context prefix has no root")
	}
	if prefixCount == 0 && root != "" {
		return "", errors.New("empty provider context prefix has a root")
	}
	if err := store.save(); err != nil {
		return "", err
	}
	parent := root
	for index, contentID := range suffix {
		item := contextItem{API: api, Parent: parent, Content: contentID}
		id := contextItemID(item)
		if _, err := store.tx.ExecContext(store.ctx, `INSERT INTO sampler_context(id,api,parent_id,content_id,item_count)
			VALUES($1,$2,$3,$4,$5) ON CONFLICT(id) DO NOTHING`, id, api, nullableContextID(parent), contentID, prefixCount+index+1); err != nil {
			return "", err
		}
		parent = id
	}
	return parent, nil
}

func validateCompletedTranscriptRoots(ctx context.Context, db *sql.DB) error {
	var missing string
	err := db.QueryRowContext(ctx, `SELECT id::text FROM sampler_conversation WHERE transcript_control IS NULL ORDER BY id LIMIT 1`).Scan(&missing)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	return fmt.Errorf("legacy transcript table is absent but conversation %s has no completed transcript marker", missing)
}

func migrateActiveContexts(ctx context.Context, db *sql.DB, batchSize int) error {
	for {
		var conversationID string
		err := db.QueryRowContext(ctx, `SELECT id::text FROM sampler_conversation
			WHERE context IS NOT NULL AND context <> 'null'::jsonb AND active_control IS NULL ORDER BY id LIMIT 1`).Scan(&conversationID)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("find conversation with unconverted active context: %w", err)
		}
		if err := convertActiveContextBatch(ctx, db, conversationID, batchSize); err != nil {
			return err
		}
	}
}

func convertActiveContextBatch(ctx context.Context, db *sql.DB, conversationID string, batchSize int) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	var api protocol.APIStyle
	var raw, rootRaw []byte
	if err := tx.QueryRowContext(ctx, `SELECT api,context,active_context_id FROM sampler_conversation WHERE id=$1 FOR UPDATE`, conversationID).
		Scan(&api, &raw, &rootRaw); err != nil {
		return fmt.Errorf("read active context for conversation %s: %w", conversationID, err)
	}
	var root string
	if rootRaw != nil {
		root = string(rootRaw)
	}
	native, err := decodeNativeContext(api, raw)
	if err != nil {
		return fmt.Errorf("decode active context for conversation %s: %w", conversationID, err)
	}
	records, err := native.Records()
	if err != nil {
		return fmt.Errorf("validate active context for conversation %s: %w", conversationID, err)
	}
	store := newContentStore(ctx, tx)
	prefix, err := store.loadContext(root)
	if err != nil {
		return fmt.Errorf("load active context prefix for conversation %s: %w", conversationID, err)
	}
	if len(prefix) > len(records) {
		return fmt.Errorf("active context for conversation %s has %d converted records but legacy context has %d", conversationID, len(prefix), len(records))
	}
	if len(prefix) > 0 {
		stored, err := store.restoreRecords(api, prefix)
		if err != nil {
			return fmt.Errorf("restore active context prefix for conversation %s: %w", conversationID, err)
		}
		for i := range stored {
			if !jsonEquivalent(records[i], stored[i]) {
				return fmt.Errorf("active context prefix item %d changed for conversation %s", i+1, conversationID)
			}
		}
	}
	end := min(len(records), len(prefix)+batchSize)
	suffix := make([]storage.ContentID, 0, end-len(prefix))
	for i := len(prefix); i < end; i++ {
		id, err := store.storeRecord(api, records[i])
		if err != nil {
			return fmt.Errorf("convert active context item %d for conversation %s: %w", i+1, conversationID, err)
		}
		suffix = append(suffix, id)
	}
	newRoot, err := appendContextIDs(store, api, root, len(prefix), suffix)
	if err != nil {
		return fmt.Errorf("append active context for conversation %s: %w", conversationID, err)
	}
	if len(suffix) > 0 {
		stored, err := store.restoreRecords(api, suffix)
		if err != nil {
			return fmt.Errorf("validate converted active context for conversation %s: %w", conversationID, err)
		}
		for index, record := range stored {
			if !jsonEquivalent(records[len(prefix)+index], record) {
				return fmt.Errorf("converted active context item %d changed for conversation %s", len(prefix)+index+1, conversationID)
			}
		}
	}
	if end == len(records) {
		control, err := nativeContextControl(native)
		if err != nil {
			return fmt.Errorf("store active context control for conversation %s: %w", conversationID, err)
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_conversation SET active_context_id=$2,active_control=$3 WHERE id=$1`,
			conversationID, nullableContextID(newRoot), []byte(control)); err != nil {
			return fmt.Errorf("finish active context conversion for conversation %s: %w", conversationID, err)
		}
	} else if len(suffix) > 0 {
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_conversation SET active_context_id=$2 WHERE id=$1`,
			conversationID, nullableContextID(newRoot)); err != nil {
			return fmt.Errorf("checkpoint active context conversion for conversation %s: %w", conversationID, err)
		}
	} else {
		return fmt.Errorf("active context for conversation %s made no conversion progress", conversationID)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit active context conversion for conversation %s: %w", conversationID, err)
	}
	return nil
}

type legacyProviderRequest struct {
	Kind          PendingIntentKind `json:"kind"`
	API           protocol.APIStyle `json:"api"`
	Connection    string            `json:"connection"`
	ProviderModel string            `json:"provider_model"`
	Method        string            `json:"method"`
	URL           string            `json:"url"`
	Headers       map[string]string `json:"headers"`
	Body          json.RawMessage   `json:"body"`
	WireRequest   string            `json:"wire_request"`
	ContextID     string            `json:"context_id"`
	ProfileID     storage.ContentID `json:"profile_id"`
}

type modelRequestOrder struct {
	id            string
	dispatch      uint64
	hasDispatch   bool
	contextLength int64
	pending       bool
	legacy        bool
}

func migrateProviderRequests(ctx context.Context, db *sql.DB, batchSize int) error {
	conversations, err := providerRequestConversations(ctx, db)
	if err != nil {
		return err
	}
	for _, conversationID := range conversations {
		entries, sessionID, err := modelRequestOrderForConversation(ctx, db, conversationID)
		if err != nil {
			return err
		}
		if len(entries) == 0 {
			continue
		}
		var dispatches map[string]uint64
		if sessionID != "" {
			tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
			if err != nil {
				return err
			}
			dispatches, err = legacyDispatchSequences(ctx, tx, sessionID)
			if err != nil {
				_ = tx.Rollback()
				return fmt.Errorf("scan dispatch order for conversation %s: %w", conversationID, err)
			}
			if err := tx.Commit(); err != nil {
				return err
			}
		}
		for i := range entries {
			if sequence, ok := dispatches[entries[i].id]; ok {
				entries[i].dispatch = sequence
				entries[i].hasDispatch = true
			}
			if entries[i].pending && !entries[i].hasDispatch {
				return fmt.Errorf("pending provider intent %s in conversation %s has no historical dispatch evidence", entries[i].id, conversationID)
			}
		}
		fallbackCount := 0
		for _, entry := range entries {
			if !entry.hasDispatch {
				fallbackCount++
			}
		}
		if fallbackCount > 0 {
			slog.WarnContext(ctx, "historical provider requests lack journal dispatch order; ordering resolved requests by context length",
				"conversation_id", conversationID, "request_count", fallbackCount)
		}
		sort.Slice(entries, func(i, j int) bool {
			left, right := entries[i], entries[j]
			if left.hasDispatch != right.hasDispatch {
				return !left.hasDispatch
			}
			if left.hasDispatch && left.dispatch != right.dispatch {
				return left.dispatch < right.dispatch
			}
			if left.contextLength != right.contextLength {
				return left.contextLength < right.contextLength
			}
			return left.id < right.id
		})
		if fallbackCount > 0 {
			if err := validateFallbackProfileOrder(ctx, db, conversationID, entries, fallbackCount); err != nil {
				return err
			}
		}
		for start := 0; start < len(entries); start += batchSize {
			end := min(start+batchSize, len(entries))
			tx, err := db.BeginTx(ctx, nil)
			if err != nil {
				return err
			}
			for i := start; i < end; i++ {
				if err := convertProviderRequest(ctx, tx, conversationID, entries[i], int64(i+1)); err != nil {
					_ = tx.Rollback()
					return err
				}
			}
			if err := tx.Commit(); err != nil {
				return fmt.Errorf("commit provider request batch for conversation %s: %w", conversationID, err)
			}
		}
	}
	if _, err := db.ExecContext(ctx, `SELECT setval(pg_get_serial_sequence('sampler_model_request','position'),
		COALESCE(max(position),1),max(position) IS NOT NULL) FROM sampler_model_request`); err != nil {
		return fmt.Errorf("advance provider request position sequence: %w", err)
	}
	return nil
}

func providerRequestConversations(ctx context.Context, db *sql.DB) ([]string, error) {
	rows, err := db.QueryContext(ctx, `SELECT DISTINCT conversation_id::text FROM sampler_intent
		WHERE request->>'kind' IN ('sampling','compaction') AND
			(request ? 'body' OR request ? 'wire_request' OR request->>'api' IN ('responses','anthropic_messages'))
		ORDER BY conversation_id`)
	if err != nil {
		return nil, fmt.Errorf("find provider request conversations: %w", err)
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return ids, nil
}

func modelRequestOrderForConversation(ctx context.Context, db *sql.DB, conversationID string) ([]modelRequestOrder, string, error) {
	var sessionID string
	if err := db.QueryRowContext(ctx, `SELECT id FROM sampler_session WHERE conversation_id=$1`, conversationID).Scan(&sessionID); err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, "", fmt.Errorf("find session for conversation %s: %w", conversationID, err)
	}
	var entries []modelRequestOrder
	var after string
	for {
		query := `SELECT i.id::text,i.request,i.result IS NULL,
			mr.id IS NOT NULL,COALESCE(c.item_count,0)
			FROM sampler_intent i LEFT JOIN sampler_model_request mr ON mr.id=i.id
			LEFT JOIN sampler_context c ON c.id=mr.context_id
			WHERE i.conversation_id=$1 AND i.request->>'kind' IN ('sampling','compaction')
			AND (i.request ? 'body' OR i.request ? 'wire_request' OR i.request->>'api' IN ('responses','anthropic_messages'))
			AND ($2='' OR i.id>NULLIF($2,'')::uuid) ORDER BY i.id LIMIT $3`
		rows, err := db.QueryContext(ctx, query, conversationID, after, intentMigrationBatchSize)
		if err != nil {
			return nil, "", fmt.Errorf("read provider request order for conversation %s: %w", conversationID, err)
		}
		count := 0
		for rows.Next() {
			var id string
			var raw []byte
			var pending, stored bool
			var contextCount int64
			if err := rows.Scan(&id, &raw, &pending, &stored, &contextCount); err != nil {
				_ = rows.Close()
				return nil, "", err
			}
			var shape legacyProviderRequest
			if err := json.Unmarshal(raw, &shape); err != nil {
				_ = rows.Close()
				return nil, "", fmt.Errorf("decode provider request metadata for intent %s: %w", id, err)
			}
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(raw, &fields); err != nil {
				_ = rows.Close()
				return nil, "", fmt.Errorf("decode provider request fields for intent %s: %w", id, err)
			}
			_, hasBody := fields["body"]
			_, hasWire := fields["wire_request"]
			legacy := hasBody || hasWire
			if legacy {
				body, err := legacyRequestBody(shape)
				if err != nil {
					_ = rows.Close()
					return nil, "", fmt.Errorf("read body for provider intent %s: %w", id, err)
				}
				contextCount, err = legacyContextLength(shape.API, body)
				if err != nil {
					_ = rows.Close()
					return nil, "", fmt.Errorf("read context length for provider intent %s: %w", id, err)
				}
			} else if stored {
				if shape.ProfileID == "" {
					_ = rows.Close()
					return nil, "", fmt.Errorf("provider intent %s has no legacy body or stored profile", id)
				}
				if shape.ContextID != "" {
					if err := db.QueryRowContext(ctx, `SELECT item_count FROM sampler_context WHERE id=$1`, shape.ContextID).Scan(&contextCount); err != nil {
						_ = rows.Close()
						return nil, "", fmt.Errorf("read stored context length for provider intent %s: %w", id, err)
					}
				}
			} else {
				_ = rows.Close()
				return nil, "", fmt.Errorf("provider intent %s is missing both legacy body and native request row", id)
			}
			entries = append(entries, modelRequestOrder{id: id, contextLength: contextCount, pending: pending, legacy: legacy})
			after = id
			count++
		}
		rowsErr := rows.Err()
		closeErr := rows.Close()
		if rowsErr != nil {
			return nil, "", rowsErr
		}
		if closeErr != nil {
			return nil, "", closeErr
		}
		if count == 0 || count < intentMigrationBatchSize {
			break
		}
	}
	return entries, sessionID, nil
}

func validateFallbackProfileOrder(ctx context.Context, db *sql.DB, conversationID string, entries []modelRequestOrder, fallbackCount int) error {
	if fallbackCount <= 0 || len(entries) <= 1 {
		return nil
	}
	lastDispatched := entries[len(entries)-1]
	knownProfile, err := staticRequestProfile(ctx, db, lastDispatched.id)
	if err != nil {
		return err
	}
	for _, fallback := range entries[:fallbackCount] {
		profile, err := staticRequestProfile(ctx, db, fallback.id)
		if err != nil {
			return err
		}
		if !jsonEquivalent(knownProfile, profile) {
			return fmt.Errorf("conversation %s has ambiguous historical dispatch order between provider intents %s and %s with different static request profiles",
				conversationID, fallback.id, lastDispatched.id)
		}
	}
	return nil
}

// staticRequestProfile removes only the ordered input/messages array. The remaining provider
// request is the frozen profile whose latest value controls future signed continuations.
func staticRequestProfile(ctx context.Context, db *sql.DB, intentID string) ([]byte, error) {
	var raw []byte
	if err := db.QueryRowContext(ctx, `SELECT request FROM sampler_intent WHERE id=$1`, intentID).Scan(&raw); err != nil {
		return nil, fmt.Errorf("read profile for historical intent %s: %w", intentID, err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return nil, fmt.Errorf("decode profile for historical intent %s: %w", intentID, err)
	}
	var body []byte
	_, legacyBody := fields["body"]
	_, legacyWire := fields["wire_request"]
	if legacyBody || legacyWire {
		var legacy legacyProviderRequest
		if err := json.Unmarshal(raw, &legacy); err != nil {
			return nil, fmt.Errorf("decode legacy profile for intent %s: %w", intentID, err)
		}
		parsed, err := legacyRequestBody(legacy)
		if err != nil {
			return nil, fmt.Errorf("read legacy profile for intent %s: %w", intentID, err)
		}
		body = parsed
	} else {
		var intent IntentRequest
		if err := json.Unmarshal(raw, &intent); err != nil {
			return nil, fmt.Errorf("decode native profile for intent %s: %w", intentID, err)
		}
		if intent.ProviderRequest == nil {
			return nil, fmt.Errorf("native profile for intent %s has no provider request", intentID)
		}
		tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
		if err != nil {
			return nil, err
		}
		body, err = renderProviderRequest(newContentStore(ctx, tx), *intent.ProviderRequest)
		_ = tx.Rollback()
		if err != nil {
			return nil, fmt.Errorf("render native profile for intent %s: %w", intentID, err)
		}
	}
	var request map[string]json.RawMessage
	if err := json.Unmarshal(body, &request); err != nil || request == nil {
		return nil, fmt.Errorf("provider request profile for intent %s is not an object", intentID)
	}
	delete(request, "input")
	delete(request, "messages")
	profile, err := json.Marshal(request)
	if err != nil {
		return nil, fmt.Errorf("encode provider request profile for intent %s: %w", intentID, err)
	}
	return profile, nil
}

func legacyRequestBody(request legacyProviderRequest) (json.RawMessage, error) {
	body := bytes.TrimSpace(request.Body)
	if len(body) > 0 && !bytes.Equal(body, []byte("null")) {
		if request.WireRequest != "" && !jsonEquivalent(body, []byte(request.WireRequest)) {
			return nil, errors.New("stored body and wire request are not semantically equal")
		}
		return append(json.RawMessage(nil), body...), nil
	}
	if request.WireRequest == "" {
		return nil, errors.New("provider request has no body")
	}
	wire := []byte(request.WireRequest)
	if !validJSON(wire) {
		return nil, errors.New("provider wire request is not valid JSON")
	}
	return append(json.RawMessage(nil), wire...), nil
}

func legacyContextLength(api protocol.APIStyle, body json.RawMessage) (int64, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil || fields == nil {
		return 0, errors.New("provider request body must be a JSON object")
	}
	field := "input"
	if api == protocol.APIStyleAnthropicMessages {
		field = "messages"
	} else if api != protocol.APIStyleResponses {
		return 0, fmt.Errorf("unsupported provider API %q", api)
	}
	raw, ok := fields[field]
	if !ok {
		return 0, fmt.Errorf("provider request body has no %q array", field)
	}
	var records []json.RawMessage
	if err := json.Unmarshal(raw, &records); err != nil || records == nil && !bytes.Equal(bytes.TrimSpace(raw), []byte("[]")) {
		return 0, fmt.Errorf("provider request %q field must be an array", field)
	}
	return int64(len(records)), nil
}

func convertProviderRequest(ctx context.Context, tx *sql.Tx, conversationID string, order modelRequestOrder, position int64) error {
	var raw []byte
	if err := tx.QueryRowContext(ctx, `SELECT request FROM sampler_intent WHERE id=$1`, order.id).Scan(&raw); err != nil {
		return fmt.Errorf("read provider intent %s: %w", order.id, err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return fmt.Errorf("decode provider intent %s: %w", order.id, err)
	}
	_, hasBody := fields["body"]
	_, hasWire := fields["wire_request"]
	if hasBody || hasWire {
		var old legacyProviderRequest
		if err := json.Unmarshal(raw, &old); err != nil {
			return fmt.Errorf("decode legacy provider intent %s: %w", order.id, err)
		}
		body, err := legacyRequestBody(old)
		if err != nil {
			return fmt.Errorf("validate legacy provider intent %s: %w", order.id, err)
		}
		metadata := ProviderRequest{
			API: old.API, Connection: old.Connection, ProviderModel: old.ProviderModel,
			Method: old.Method, URL: old.URL, Headers: old.Headers,
		}
		request, wire, err := packLegacyRequest(ctx, tx, metadata, body)
		if err != nil {
			return fmt.Errorf("convert provider intent %s: %w", order.id, err)
		}
		if !jsonEquivalent(body, wire) {
			return fmt.Errorf("converted provider request for intent %s is not semantically equivalent", order.id)
		}
		if err := setCompactIntentFields(fields, &request); err != nil {
			return fmt.Errorf("encode compact provider intent %s: %w", order.id, err)
		}
		delete(fields, "body")
		delete(fields, "wire_request")
		compact, err := json.Marshal(fields)
		if err != nil {
			return fmt.Errorf("encode provider intent %s: %w", order.id, err)
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET request=$2 WHERE id=$1`, order.id, compact); err != nil {
			return fmt.Errorf("store compact provider intent %s: %w", order.id, err)
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_model_request(id,conversation_id,position,context_id,profile_id)
			VALUES($1,$2,$3,$4,$5) ON CONFLICT(id) DO UPDATE SET conversation_id=EXCLUDED.conversation_id,
			position=EXCLUDED.position,context_id=EXCLUDED.context_id,profile_id=EXCLUDED.profile_id`,
			order.id, conversationID, position, nullableContextID(request.ContextID), request.ProfileID); err != nil {
			return fmt.Errorf("store native provider request %s: %w", order.id, err)
		}
		return nil
	}
	var compact IntentRequest
	if err := json.Unmarshal(raw, &compact); err != nil || compact.ProviderRequest == nil || compact.ProfileID == "" {
		return fmt.Errorf("provider intent %s has an incomplete native request", order.id)
	}
	if _, err := tx.ExecContext(ctx, `UPDATE sampler_model_request SET position=$2 WHERE id=$1`, order.id, position); err != nil {
		return fmt.Errorf("set provider request order for intent %s: %w", order.id, err)
	}
	return nil
}

func setCompactIntentFields(fields map[string]json.RawMessage, request *ProviderRequest) error {
	encoded, err := json.Marshal(request)
	if err != nil {
		return err
	}
	var compact map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &compact); err != nil {
		return err
	}
	for name, raw := range compact {
		fields[name] = raw
	}
	return nil
}

func validJSON(raw []byte) bool {
	_, err := protocol.DecodeJSONValue(raw)
	return err == nil
}

func jsonEquivalent(left, right []byte) bool {
	a, err := protocol.DecodeJSONValue(left)
	if err != nil {
		return false
	}
	b, err := protocol.DecodeJSONValue(right)
	return err == nil && jsonValuesEqual(a, b)
}

func jsonValuesEqual(left, right any) bool {
	switch a := left.(type) {
	case json.Number:
		b, ok := right.(json.Number)
		if !ok {
			return false
		}
		leftRat, leftOK := decimalRat(a)
		rightRat, rightOK := decimalRat(b)
		return leftOK && rightOK && leftRat.Cmp(rightRat) == 0
	case []any:
		b, ok := right.([]any)
		if !ok || len(a) != len(b) {
			return false
		}
		for i := range a {
			if !jsonValuesEqual(a[i], b[i]) {
				return false
			}
		}
		return true
	case map[string]any:
		b, ok := right.(map[string]any)
		if !ok || len(a) != len(b) {
			return false
		}
		for key, value := range a {
			other, ok := b[key]
			if !ok || !jsonValuesEqual(value, other) {
				return false
			}
		}
		return true
	default:
		return reflect.DeepEqual(left, right)
	}
}

func decimalRat(number json.Number) (*big.Rat, bool) {
	value, ok := new(big.Rat).SetString(string(number))
	return value, ok
}

func validateNativeContent(ctx context.Context, db *sql.DB, oldItems, oldContext bool) error {
	var badTranscript string
	err := db.QueryRowContext(ctx, `SELECT id::text FROM sampler_conversation WHERE transcript_control IS NULL ORDER BY id LIMIT 1`).Scan(&badTranscript)
	if !errors.Is(err, sql.ErrNoRows) {
		if err != nil {
			return err
		}
		return fmt.Errorf("conversation %s has no completed transcript context", badTranscript)
	}
	if oldItems {
		rows, err := db.QueryContext(ctx, `SELECT c.id::text,c.api,COALESCE(c.transcript_context_id,''),count(i.ordinal),COALESCE(max(i.ordinal),0)
			FROM sampler_conversation c LEFT JOIN sampler_conversation_item i ON i.conversation_id=c.id
			GROUP BY c.id,c.api,c.transcript_context_id ORDER BY c.id`)
		if err != nil {
			return fmt.Errorf("validate converted transcripts: %w", err)
		}
		for rows.Next() {
			var conversationID, root string
			var api protocol.APIStyle
			var count, maxOrdinal int64
			if err := rows.Scan(&conversationID, &api, &root, &count, &maxOrdinal); err != nil {
				_ = rows.Close()
				return err
			}
			if count != maxOrdinal {
				_ = rows.Close()
				return fmt.Errorf("legacy transcript for conversation %s is not contiguous", conversationID)
			}
			tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
			if err != nil {
				_ = rows.Close()
				return err
			}
			ids, err := newContentStore(ctx, tx).loadContext(root)
			_ = tx.Rollback()
			if err != nil || int64(len(ids)) != count {
				_ = rows.Close()
				if err != nil {
					return fmt.Errorf("validate transcript root for conversation %s: %w", conversationID, err)
				}
				return fmt.Errorf("transcript root for conversation %s contains %d items; legacy transcript has %d", conversationID, len(ids), count)
			}
		}
		rowsErr := rows.Err()
		closeErr := rows.Close()
		if rowsErr != nil {
			return rowsErr
		}
		if closeErr != nil {
			return closeErr
		}
	}
	if oldContext {
		var missing string
		err := db.QueryRowContext(ctx, `SELECT id::text FROM sampler_conversation
			WHERE context IS NOT NULL AND context <> 'null'::jsonb AND active_control IS NULL ORDER BY id LIMIT 1`).Scan(&missing)
		if !errors.Is(err, sql.ErrNoRows) {
			if err != nil {
				return err
			}
			return fmt.Errorf("conversation %s has an unconverted active context", missing)
		}
	}
	var invalidActive string
	err = db.QueryRowContext(ctx, `SELECT id::text FROM sampler_conversation
		WHERE (active_context_id IS NOT NULL AND active_control IS NULL)
		OR (active_control IS NOT NULL AND jsonb_typeof(active_control)<>'object')
		ORDER BY id LIMIT 1`).Scan(&invalidActive)
	if !errors.Is(err, sql.ErrNoRows) {
		if err != nil {
			return err
		}
		return fmt.Errorf("conversation %s has an incomplete active context root", invalidActive)
	}
	var missingRequest string
	err = db.QueryRowContext(ctx, `SELECT i.id::text FROM sampler_intent i LEFT JOIN sampler_model_request r ON r.id=i.id
		WHERE i.request->>'kind' IN ('sampling','compaction') AND
			(i.request ? 'body' OR i.request ? 'wire_request' OR i.request->>'api' IN ('responses','anthropic_messages'))
			AND (r.id IS NULL OR i.request ? 'body' OR i.request ? 'wire_request')
		ORDER BY i.id LIMIT 1`).Scan(&missingRequest)
	if !errors.Is(err, sql.ErrNoRows) {
		if err != nil {
			return err
		}
		return fmt.Errorf("provider intent %s was not converted to native request storage", missingRequest)
	}
	if err := validateStoredProviderRequests(ctx, db); err != nil {
		return err
	}
	return nil
}

func validateStoredProviderRequests(ctx context.Context, db *sql.DB) error {
	rows, err := db.QueryContext(ctx, `SELECT i.id::text,i.request FROM sampler_intent i
		JOIN sampler_model_request r ON r.id=i.id WHERE i.request->>'kind' IN ('sampling','compaction') AND
			(i.request ? 'body' OR i.request ? 'wire_request' OR i.request->>'api' IN ('responses','anthropic_messages'))
		ORDER BY i.id`)
	if err != nil {
		return err
	}
	type requestRow struct {
		id  string
		raw []byte
	}
	var requests []requestRow
	for rows.Next() {
		var row requestRow
		if err := rows.Scan(&row.id, &row.raw); err != nil {
			_ = rows.Close()
			return err
		}
		requests = append(requests, row)
	}
	rowsErr := rows.Err()
	closeErr := rows.Close()
	if rowsErr != nil {
		return rowsErr
	}
	if closeErr != nil {
		return closeErr
	}
	for _, row := range requests {
		var intent IntentRequest
		if err := json.Unmarshal(row.raw, &intent); err != nil {
			return fmt.Errorf("decode native provider intent %s during validation: %w", row.id, err)
		}
		if intent.ProviderRequest == nil {
			return fmt.Errorf("native provider intent %s has no provider request", row.id)
		}
		request := intent.ProviderRequest
		tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
		if err != nil {
			return err
		}
		_, bodyErr := renderProviderRequest(newContentStore(ctx, tx), *request)
		rollbackErr := tx.Rollback()
		if bodyErr != nil {
			return fmt.Errorf("validate native provider request %s: %w", row.id, bodyErr)
		}
		if rollbackErr != nil && !errors.Is(rollbackErr, sql.ErrTxDone) {
			return rollbackErr
		}
	}
	return nil
}

// legacyDispatchSequences reads provider dispatch evidence in the order the sampler journal
// committed it. Conversation-item ordinals and intent UUIDs are not an ordering source: the
// journal is the authoritative ordering for provider requests.
func legacyDispatchSequences(ctx context.Context, tx *sql.Tx, sessionID string) (map[string]uint64, error) {
	return scanLegacyDispatchSequences(ctx, tx, sessionID, legacyDispatchScanPageSize)
}

// scanLegacyDispatchSequences keeps each database read bounded. The page-size parameter exists
// separately so the pagination path can be exercised with small PostgreSQL fixtures.
func scanLegacyDispatchSequences(ctx context.Context, tx *sql.Tx, sessionID string, pageSize int) (map[string]uint64, error) {
	if tx == nil || sessionID == "" || pageSize <= 0 {
		return nil, fmt.Errorf("scan legacy dispatches requires a transaction, session ID, and positive page size")
	}
	dispatches := make(map[string]uint64)
	var after int64
	hasAfter := false
	for {
		var rows *sql.Rows
		var err error
		if hasAfter {
			rows, err = tx.QueryContext(ctx, `SELECT sequence, entry FROM sampler_event
				WHERE session_id=$1 AND sequence>$2 ORDER BY sequence LIMIT $3`, sessionID, after, pageSize)
		} else {
			rows, err = tx.QueryContext(ctx, `SELECT sequence, entry FROM sampler_event
				WHERE session_id=$1 ORDER BY sequence LIMIT $2`, sessionID, pageSize)
		}
		if err != nil {
			return nil, err
		}
		pageCount := 0
		for rows.Next() {
			var sequence int64
			var wire []byte
			if err := rows.Scan(&sequence, &wire); err != nil {
				_ = rows.Close()
				return nil, err
			}
			if sequence <= 0 || (hasAfter && sequence <= after) {
				_ = rows.Close()
				return nil, fmt.Errorf("invalid sampler journal sequence %d for session %q", sequence, sessionID)
			}
			after = sequence
			hasAfter = true
			pageCount++
			entry := &eventlogpb.EventEntry{}
			if err := decodeStoredEvent(wire, entry); err != nil {
				_ = rows.Close()
				return nil, fmt.Errorf("decode sampler journal entry at sequence %d: %w", sequence, err)
			}
			boundary := entry.GetEvent().GetBoundaryRecord()
			if boundary == nil || boundary.GetDirection() != "dispatch" || boundary.GetCorrelationId() == "" ||
				(boundary.GetBoundary() != "sampling" && boundary.GetBoundary() != "compaction") || boundary.GetFragmentIndex() != 0 {
				continue
			}
			id := boundary.GetCorrelationId()
			if _, exists := dispatches[id]; !exists {
				dispatches[id] = uint64(sequence)
			}
		}
		rowsErr := rows.Err()
		closeErr := rows.Close()
		if rowsErr != nil {
			return nil, rowsErr
		}
		if closeErr != nil {
			return nil, closeErr
		}
		if pageCount == 0 || pageCount < pageSize {
			return dispatches, nil
		}
	}
}
