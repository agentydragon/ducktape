package projection

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/history_service/threadfold"
	"github.com/google/uuid"
)

// payloadPlan is the manifest and chunk one payload revision gets: prefixChunks and prefixBytes are what the
// base revision spans, body is the text added on top of them (the whole text when replaced).
type payloadPlan struct {
	reference    threadfold.PayloadRef
	prefixChunks uint64
	prefixBytes  uint64
	// body is shared by the plans of one batch's chain of writes to the same body.
	body     *strings.Builder
	replaced bool
}

type payloadOwnerField struct {
	ownerCursor uint64
	ownerID     string
	field       threadfold.PayloadField
}

// writePayloads stores a batch's payload bodies insert-only: a manifest row per revision and a chunk row per
// generation, so a body once written never changes. Of the writes to one body in a batch only the last
// revision is stored, holding everything the earlier ones added.
func writePayloads(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, writes []threadfold.PayloadWrite) error {
	plansByReference := map[threadfold.PayloadRef]*payloadPlan{}
	var finalOrder []payloadOwnerField
	finalPlans := map[payloadOwnerField]*payloadPlan{}
	for _, write := range writes {
		var plan *payloadPlan
		switch write := write.(type) {
		case threadfold.ReplacePayload:
			plan = &payloadPlan{reference: write.Reference, body: &strings.Builder{}, replaced: true}
			plan.body.WriteString(write.Text)
		case threadfold.AppendPayload:
			var prior *payloadPlan
			if write.Base != nil {
				prior = plansByReference[*write.Base]
			}
			if prior != nil {
				prior.body.WriteString(write.Text)
				plan = &payloadPlan{write.Reference, prior.prefixChunks, prior.prefixBytes, prior.body, prior.replaced}
				break
			}
			plan = &payloadPlan{reference: write.Reference, body: &strings.Builder{}}
			if write.Base != nil {
				chunks, bytes, err := manifestExtent(ctx, tx, threadID, *write.Base)
				if err != nil {
					return err
				}
				plan.prefixChunks, plan.prefixBytes = chunks, bytes
			}
			plan.body.WriteString(write.Text)
		default:
			return fmt.Errorf("unknown payload write %T", write)
		}
		plansByReference[plan.reference] = plan
		key := payloadOwnerField{plan.reference.OwnerCursor, plan.reference.OwnerID, plan.reference.Field}
		if _, seen := finalPlans[key]; !seen {
			finalOrder = append(finalOrder, key)
		}
		finalPlans[key] = plan
	}
	for _, key := range finalOrder {
		if err := storePayload(ctx, tx, threadID, finalPlans[key]); err != nil {
			return err
		}
	}
	return nil
}

func manifestExtent(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, reference threadfold.PayloadRef) (chunks, bytes uint64, err error) {
	err = tx.QueryRowContext(ctx, `SELECT chunk_count, content_bytes FROM thread_payload_manifest
		WHERE thread_id = $1 AND projection_epoch = $2 AND owner_cursor = $3 AND owner_id = $4 AND field = $5
		AND generation = $6 AND revision_cursor = $7`,
		threadID, reference.ProjectionEpoch, reference.OwnerCursor, reference.OwnerID, reference.Field,
		reference.Generation, reference.RevisionCursor).Scan(&chunks, &bytes)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, 0, fmt.Errorf("%w: append references a missing payload manifest", ErrInconsistentFold)
	}
	return chunks, bytes, err
}

func storePayload(ctx context.Context, tx *sql.Tx, threadID uuid.UUID, plan *payloadPlan) error {
	text := plan.body.String()
	chunkCount := plan.prefixChunks
	if text != "" {
		chunkCount++
	}
	contentBytes := plan.prefixBytes + uint64(len(text))
	if plan.replaced {
		contentBytes = uint64(len(text))
	}
	reference := plan.reference
	if _, err := tx.ExecContext(ctx, `INSERT INTO thread_payload_manifest
		(thread_id, projection_epoch, owner_cursor, owner_id, field, generation, revision_cursor, chunk_count, content_bytes)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
		threadID, reference.ProjectionEpoch, reference.OwnerCursor, reference.OwnerID, reference.Field,
		reference.Generation, reference.RevisionCursor, chunkCount, contentBytes); err != nil {
		return err
	}
	if chunkCount == plan.prefixChunks {
		return nil
	}
	encoded, err := jsonString(text)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO thread_payload_chunk
		(thread_id, projection_epoch, owner_cursor, owner_id, field, generation, chunk_index, text)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8::json)`,
		threadID, reference.ProjectionEpoch, reference.OwnerCursor, reference.OwnerID, reference.Field,
		reference.Generation, plan.prefixChunks, encoded)
	return err
}

// jsonString encodes text as a JSON string scalar, which keeps U+0000 where a text column cannot. HTML
// characters stay literal: a body full of `<` and `&` would otherwise grow sixfold.
func jsonString(text string) (string, error) {
	var encoded strings.Builder
	encoder := json.NewEncoder(&encoded)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(text); err != nil {
		return "", err
	}
	return strings.TrimSuffix(encoded.String(), "\n"), nil
}
