package migrations

import (
	"context"
	"database/sql"
	"fmt"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/pressly/goose/v3"
	"google.golang.org/protobuf/proto"
)

// Existing central openings must retain their owner when the typed column becomes authoritative.
// Decode the stored protobuf instead of relying on its wire field order in SQL.
var sessionPlacementMigration = goose.NewGoMigration(11,
	&goose.GoFunc{RunTx: func(ctx context.Context, tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, "ALTER TABLE session_opening ADD COLUMN placement VARCHAR"); err != nil {
			return err
		}
		rows, err := tx.QueryContext(ctx, "SELECT session_id, spec FROM session_opening")
		if err != nil {
			return err
		}
		type opening struct {
			id, placement string
		}
		var openings []opening
		for rows.Next() {
			var id string
			var data []byte
			if err := rows.Scan(&id, &data); err != nil {
				rows.Close()
				return err
			}
			spec := &sessionpb.SessionSpec{}
			if err := proto.Unmarshal(data, spec); err != nil {
				rows.Close()
				return fmt.Errorf("decode opening %s: %w", id, err)
			}
			placement := "environment_hosted"
			if spec.GetHarness() == sessionpb.Harness_HARNESS_CENTRAL_SAMPLER {
				placement = "central_sampler"
			}
			openings = append(openings, opening{id, placement})
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		if err := rows.Close(); err != nil {
			return err
		}
		for _, row := range openings {
			if _, err := tx.ExecContext(ctx, "UPDATE session_opening SET placement=$2 WHERE session_id=$1", row.id, row.placement); err != nil {
				return err
			}
		}
		_, err = tx.ExecContext(ctx, "ALTER TABLE session_opening ALTER COLUMN placement SET NOT NULL")
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, "ALTER TABLE session_opening ADD CONSTRAINT session_opening_placement_check CHECK (placement IN ('environment_hosted', 'central_sampler'))")
		return err
	}},
	&goose.GoFunc{RunTx: func(ctx context.Context, tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, "ALTER TABLE session_opening DROP COLUMN placement")
		return err
	}})
