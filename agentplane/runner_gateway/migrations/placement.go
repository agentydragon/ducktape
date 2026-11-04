package migrations

import (
	"context"
	"database/sql"
	"fmt"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/pressly/goose/v3"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// Version 11 converts accepted opening receipts in place. It never requeues work. The legacy
// harness tag is removed only after its backend has become part of the immutable placement.
var sessionPlacementMigration = goose.NewGoMigration(11,
	&goose.GoFunc{RunTx: func(ctx context.Context, tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, `SELECT session_id,sandbox_name,sandbox_uid::text,spec
			FROM session_opening`)
		if err != nil {
			return err
		}
		type update struct {
			id   string
			spec []byte
		}
		var updates []update
		for rows.Next() {
			var id, name, uid string
			var raw []byte
			if err := rows.Scan(&id, &name, &uid, &raw); err != nil {
				rows.Close()
				return err
			}
			spec := &sessionpb.SessionSpec{}
			if err := proto.Unmarshal(raw, spec); err != nil {
				rows.Close()
				return fmt.Errorf("decode opening %s: %w", id, err)
			}
			harness, other, err := extractLegacyHarness(spec.ProtoReflect().GetUnknown())
			if err != nil {
				rows.Close()
				return fmt.Errorf("opening %s: %w", id, err)
			}
			target := &sessionpb.ExactEnvironment{Name: name, Uid: uid}
			switch harness {
			case sessionpb.Harness_HARNESS_CLAUDE, sessionpb.Harness_HARNESS_CODEX:
				spec.Placement = &sessionpb.SessionSpec_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: target, Harness: harness}}
			case sessionpb.Harness_HARNESS_CENTRAL_SAMPLER:
				spec.Placement = &sessionpb.SessionSpec_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: target}}
			default:
				rows.Close()
				return fmt.Errorf("opening %s has no known retained harness", id)
			}
			spec.ProtoReflect().SetUnknown(other)
			converted, err := proto.Marshal(spec)
			if err != nil {
				rows.Close()
				return err
			}
			updates = append(updates, update{id, converted})
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		if err := rows.Close(); err != nil {
			return err
		}
		for _, row := range updates {
			if _, err := tx.ExecContext(ctx, "UPDATE session_opening SET spec=$2 WHERE session_id=$1", row.id, row.spec); err != nil {
				return err
			}
		}
		return nil
	}},
	&goose.GoFunc{RunTx: func(context.Context, *sql.Tx) error {
		return fmt.Errorf("Session placement migration cannot be reversed without losing placement identity")
	}})

// extractLegacyHarness moves only tag 1; unknown future fields survive byte-for-byte.
func extractLegacyHarness(data []byte) (sessionpb.Harness, []byte, error) {
	var harness sessionpb.Harness
	var other []byte
	for len(data) != 0 {
		number, kind, n := protowire.ConsumeTag(data)
		if n < 0 {
			return 0, nil, protowire.ParseError(n)
		}
		length := protowire.ConsumeFieldValue(number, kind, data[n:])
		if length < 0 {
			return 0, nil, protowire.ParseError(length)
		}
		field := data[:n+length]
		if number == 1 {
			if kind != protowire.VarintType {
				return 0, nil, fmt.Errorf("legacy harness tag has wrong wire type")
			}
			value, _ := protowire.ConsumeVarint(data[n:])
			harness = sessionpb.Harness(value)
		} else {
			other = append(other, field...)
		}
		data = data[n+length:]
	}
	return harness, other, nil
}
