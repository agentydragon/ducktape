package storage

import (
	"context"
	"database/sql"

	"github.com/agentydragon/ducktape/agentplane/eventcatalog"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
)

// insertCatalogEvent keeps one copy of an exact catalog per Session. A hash collision falls
// back to the ordinary event row, preserving both catalog values.
func insertCatalogEvent(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, entry *eventlogpb.EventEntry, wire []byte, kind int32) (bool, error) {
	shortened, catalog, offset := eventcatalog.Split(entry, wire)
	if catalog == nil {
		return false, nil
	}
	key := eventcatalog.Key(catalog)
	var matches bool
	// The Session lock serializes inserts of this key, so the statement sees any prior value.
	err := tx.QueryRowContext(ctx, `WITH inserted AS (
		INSERT INTO session_event_catalog(session_id,digest,payload) VALUES($1,$2,$3)
		ON CONFLICT (session_id,digest) DO NOTHING RETURNING TRUE AS matches
	)
	SELECT matches FROM inserted
	UNION ALL SELECT payload=$3 FROM session_event_catalog WHERE session_id=$1 AND digest=$2`,
		sessionID, key[:], catalog).Scan(&matches)
	if err != nil {
		return false, err
	}
	if !matches {
		return false, nil
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO session_event
		(session_id,cursor,payload,kind,catalog_id,catalog_offset,catalog_size) VALUES($1,$2,$3,$4,$5,$6,$7)`,
		sessionID, entry.GetCursor(), shortened, kind, key[:], offset, len(catalog))
	return err == nil, err
}
