package reads

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"math"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/threadfold"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

const maxEntities = 200

// ReadThreadEntities reads a window of the Session's current Thread entities, as the views browsers sync.
func (r *Reader) ReadThreadEntities(ctx context.Context, request *historypb.ReadThreadEntitiesRequest) (*historypb.ReadThreadEntitiesResponse, error) {
	sessionID, err := parseSessionID(request.GetSessionId())
	if err != nil {
		return nil, err
	}
	// The window's predicate on the Session's rows, with its arguments from $3.
	var window string
	var arguments []any
	switch selected := request.GetWindow().(type) {
	case *historypb.ReadThreadEntitiesRequest_Range:
		start, limit := selected.Range.GetStartEntityIndex(), uint64(selected.Range.GetLimit())
		if limit < 1 || limit > maxEntities || start > math.MaxInt64-limit {
			return nil, invalid("entity window")
		}
		window, arguments = "entity_index BETWEEN $3 AND $4", []any{start, start + limit - 1}
	case *historypb.ReadThreadEntitiesRequest_Keys:
		keys := selected.Keys.GetKeys()
		if len(keys) < 1 || len(keys) > maxEntities {
			return nil, invalid("entity window")
		}
		kinds, ids := make([]string, len(keys)), make([]string, len(keys))
		for index, key := range keys {
			kind, err := threadfold.StoredEntityKind(key.GetEntityKind())
			if err != nil {
				return nil, invalid("entity kind")
			}
			kinds[index], ids[index] = string(kind), key.GetEntityId()
		}
		window, arguments = "(entity_kind, entity_id) IN (SELECT * FROM unnest($3::text[], $4::text[]))", []any{pq.Array(kinds), pq.Array(ids)}
	default:
		return nil, invalid("entity window")
	}
	var response *historypb.ReadThreadEntitiesResponse
	err = snapshot(ctx, r.DB, func(tx *sql.Tx) error {
		if err := r.requireSession(ctx, tx, sessionID); err != nil {
			return err
		}
		checkpoint, err := readCheckpoint(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		if request.GetProjectionEpoch() != "" && (checkpoint == nil || checkpoint.projectionEpoch != request.GetProjectionEpoch()) {
			return fmt.Errorf("%w: %s", ErrScopeReset, sessionID)
		}
		response = &historypb.ReadThreadEntitiesResponse{}
		if checkpoint == nil {
			return nil
		}
		response.ProjectionEpoch, response.ThroughCursor = checkpoint.projectionEpoch, checkpoint.throughCursor
		rows, err := tx.QueryContext(ctx, `SELECT entity_index, entity_kind, entity_id, cursor, revision_cursor, pending, state
			FROM thread_entity WHERE thread_id = $1 AND projection_epoch = $2 AND `+window+` ORDER BY entity_index`,
			append([]any{sessionID, checkpoint.projectionEpoch}, arguments...)...)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			entity, err := scanEntity(rows, sessionID, checkpoint.projectionEpoch)
			if err != nil {
				return err
			}
			response.Entities = append(response.Entities, entity)
		}
		return rows.Err()
	})
	return response, err
}

type checkpoint struct {
	projectionEpoch string
	sourceID        sql.Null[string]
	throughCursor   uint64
}

// readCheckpoint is nil before anything is folded.
func readCheckpoint(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID) (*checkpoint, error) {
	var found checkpoint
	err := tx.QueryRowContext(ctx, "SELECT projection_epoch, source_id, through_cursor FROM thread_checkpoint WHERE thread_id = $1",
		sessionID).Scan(&found.projectionEpoch, &found.sourceID, &found.throughCursor)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return &found, err
}

func scanEntity(rows *sql.Rows, sessionID uuid.UUID, projectionEpoch string) (*historypb.ThreadEntity, error) {
	entity := &historypb.ThreadEntity{SessionId: sessionID.String(), ProjectionEpoch: projectionEpoch}
	var kind threadfold.EntityKind
	var pending bool
	var state []byte
	if err := rows.Scan(&entity.EntityIndex, &kind, &entity.EntityId, &entity.Cursor, &entity.RevisionCursor, &pending, &state); err != nil {
		return nil, err
	}
	if err := threadfold.ParseEntity(entity, kind, pending, state); err != nil {
		return nil, fmt.Errorf("%w: %w", ErrInvalidStoredEntity, err)
	}
	return entity, nil
}
