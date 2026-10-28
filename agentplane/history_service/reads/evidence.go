package reads

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

const maxEvidencePage = 200

// ReadEntityEvidence pages through the observations an entity was folded from.
func (r *Reader) ReadEntityEvidence(ctx context.Context, request *historypb.ReadEntityEvidenceRequest) (*historypb.ReadEntityEvidenceResponse, error) {
	if request.GetLimit() < 1 || request.GetLimit() > maxEvidencePage {
		return nil, invalid("evidence page size")
	}
	sessionID, err := parseSessionID(request.GetSessionId())
	if err != nil {
		return nil, err
	}
	var response *historypb.ReadEntityEvidenceResponse
	err = snapshot(ctx, r.DB, func(tx *sql.Tx) error {
		entityCursor, _, err := findEntity(ctx, tx, sessionID, request.GetProjectionEpoch(), request.GetEntity())
		if err != nil {
			return err
		}
		rows, err := tx.QueryContext(ctx, `SELECT e.observation_cursor, EXISTS (
				SELECT FROM thread_native_link n
				WHERE n.thread_id = e.thread_id AND n.projection_epoch = e.projection_epoch
					AND n.entity_cursor = e.entity_cursor AND n.observation_cursor = e.observation_cursor)
			FROM thread_evidence e
			WHERE e.thread_id = $1 AND e.projection_epoch = $2 AND e.entity_cursor = $3 AND e.observation_cursor > $4
			ORDER BY e.observation_cursor LIMIT $5`,
			sessionID, request.GetProjectionEpoch(), entityCursor, request.GetAfterCursor(), request.GetLimit()+1)
		if err != nil {
			return err
		}
		defer rows.Close()
		response = &historypb.ReadEntityEvidenceResponse{}
		for rows.Next() {
			observation := &historypb.EvidenceObservation{}
			if err := rows.Scan(&observation.ObservationCursor, &observation.HasNative); err != nil {
				return err
			}
			response.Observations = append(response.Observations, observation)
		}
		if err := rows.Err(); err != nil {
			return err
		}
		if uint32(len(response.Observations)) > request.GetLimit() {
			response.Observations = response.Observations[:request.GetLimit()]
			response.NextAfterCursor = proto.Uint64(response.Observations[request.GetLimit()-1].ObservationCursor)
		}
		return nil
	})
	return response, err
}

// ReadNativeFrames pages through the native harness frames behind one observation of an entity. A frame whose
// entry is not retained comes back without it.
func (r *Reader) ReadNativeFrames(ctx context.Context, request *historypb.ReadNativeFramesRequest) (*historypb.ReadNativeFramesResponse, error) {
	if request.GetLimit() < 1 || request.GetLimit() > maxEvidencePage {
		return nil, invalid("native frame page size")
	}
	sessionID, err := parseSessionID(request.GetSessionId())
	if err != nil {
		return nil, err
	}
	var sequences []uint64
	var sourceID string
	err = snapshot(ctx, r.DB, func(tx *sql.Tx) error {
		var entityCursor uint64
		var err error
		if entityCursor, sourceID, err = findEntity(ctx, tx, sessionID, request.GetProjectionEpoch(), request.GetEntity()); err != nil {
			return err
		}
		var observed bool
		err = tx.QueryRowContext(ctx, `SELECT true FROM thread_evidence
			WHERE thread_id = $1 AND projection_epoch = $2 AND entity_cursor = $3 AND observation_cursor = $4`,
			sessionID, request.GetProjectionEpoch(), entityCursor, request.GetObservationCursor()).Scan(&observed)
		if errors.Is(err, sql.ErrNoRows) {
			return fmt.Errorf("%w: %s", ErrEvidenceNotFound, sessionID)
		} else if err != nil {
			return err
		}
		rows, err := tx.QueryContext(ctx, `SELECT source_sequence FROM thread_native_link
			WHERE thread_id = $1 AND projection_epoch = $2 AND entity_cursor = $3 AND observation_cursor = $4
				AND source_sequence > $5
			ORDER BY source_sequence LIMIT $6`,
			sessionID, request.GetProjectionEpoch(), entityCursor, request.GetObservationCursor(), request.GetAfterSequence(),
			request.GetLimit()+1)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var sequence uint64
			if err := rows.Scan(&sequence); err != nil {
				return err
			}
			sequences = append(sequences, sequence)
		}
		return rows.Err()
	})
	if err != nil {
		return nil, err
	}
	response := &historypb.ReadNativeFramesResponse{}
	if uint32(len(sequences)) > request.GetLimit() {
		sequences = sequences[:request.GetLimit()]
		response.NextAfterSequence = proto.Uint64(sequences[request.GetLimit()-1])
	}
	if len(sequences) == 0 {
		return response, nil
	}
	retained, err := r.History.ReadSequences(ctx, sessionID, sequences, r.NativeFrameReadBatchSize)
	if err != nil {
		return nil, err
	}
	for _, read := range retained.Reads {
		sequence := read.Sequence
		frame := &historypb.NativeFrame{SourceSequence: sequence}
		response.Frames = append(response.Frames, frame)
		// A link can name a sequence beyond the retained prefix; that frame is unavailable.
		if sequence > retained.LastCursor || read.Unstored {
			continue
		}
		if read.NextEntry == nil {
			continue
		}
		entry := read.NextEntry
		if entry.GetCursor() != sequence || entry.GetOrigin().GetSourceId() != sourceID {
			return nil, fmt.Errorf("retained history of Session %s does not match the folded native link at %d", sessionID, sequence)
		}
		if entry.GetEvent().GetNative() != nil {
			frame.Entry = entry
		}
	}
	return response, nil
}

// findEntity returns the entity's cursor and the Session's runner source, in the caller's still-current epoch.
func findEntity(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, projectionEpoch string, key *historypb.EntityKey) (uint64, string, error) {
	current, err := readCheckpoint(ctx, tx, sessionID)
	if err != nil {
		return 0, "", err
	}
	if current == nil {
		return 0, "", fmt.Errorf("%w: %s", ErrEvidenceNotFound, sessionID)
	}
	if current.projectionEpoch != projectionEpoch {
		return 0, "", fmt.Errorf("%w: %s", ErrScopeReset, sessionID)
	}
	var cursor uint64
	err = tx.QueryRowContext(ctx, `SELECT cursor FROM thread_entity
		WHERE thread_id = $1 AND projection_epoch = $2 AND entity_kind = $3 AND entity_id = $4`,
		sessionID, projectionEpoch, key.GetEntityKind(), key.GetEntityId()).Scan(&cursor)
	if errors.Is(err, sql.ErrNoRows) || err == nil && !current.sourceID.Valid {
		return 0, "", fmt.Errorf("%w: %s", ErrEvidenceNotFound, sessionID)
	}
	return cursor, current.sourceID.V, err
}
