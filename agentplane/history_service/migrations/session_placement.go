package migrations

import (
	"context"
	"database/sql"
	"fmt"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/lib/pq"
	"github.com/pressly/goose/v3"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// Archived snapshots may predate exact placement. Move their harness fact onto the history-specific
// Attached field without fabricating an environment target or changing event/order data.
var sessionPlacement = goose.NewGoMigration(8,
	&goose.GoFunc{RunTx: func(ctx context.Context, tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, `SELECT id,feed_state,sandbox_name,sandbox_uid
			FROM session_history WHERE feed_state IS NOT NULL`)
		if err != nil {
			return err
		}
		type update struct {
			id   string
			feed []byte
		}
		var updates []update
		for rows.Next() {
			var id string
			var raw []byte
			var sandboxName, sandboxUID sql.NullString
			if err := rows.Scan(&id, &raw, &sandboxName, &sandboxUID); err != nil {
				rows.Close()
				return err
			}
			feed := &historypb.SessionFeedState{}
			if err := proto.Unmarshal(raw, feed); err != nil {
				rows.Close()
				return fmt.Errorf("decode archived feed %s: %w", id, err)
			}
			attached := feed.GetAttached()
			if attached == nil || attached.GetSpec() == nil {
				continue
			}
			harness, rest, found, err := historicalHarness(attached.Spec.ProtoReflect().GetUnknown())
			if err != nil {
				rows.Close()
				return fmt.Errorf("archived feed %s: %w", id, err)
			}
			if !found {
				continue
			}
			if sandboxName.Valid && sandboxName.String != "" && sandboxUID.Valid && sandboxUID.String != "" {
				target := &sessionpb.ExactEnvironment{Name: sandboxName.String, Uid: sandboxUID.String}
				switch harness {
				case sessionpb.Harness_HARNESS_CLAUDE, sessionpb.Harness_HARNESS_CODEX:
					attached.Spec.Placement = &sessionpb.SessionSpec_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: target, Harness: harness}}
				case sessionpb.Harness_HARNESS_CENTRAL_SAMPLER:
					attached.Spec.Placement = &sessionpb.SessionSpec_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: target}}
				default:
					attached.HistoricalHarness = harness
				}
			} else {
				attached.HistoricalHarness = harness
			}
			attached.Spec.ProtoReflect().SetUnknown(rest)
			converted, err := proto.Marshal(feed)
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
			if _, err := tx.ExecContext(ctx, "UPDATE session_history SET feed_state=$2 WHERE id=$1", row.id, row.feed); err != nil {
				return err
			}
		}
		_, err = tx.ExecContext(ctx, `UPDATE thread_history_summary AS summary SET attached =
			jsonb_set(summary.attached, '{spec}'::text[],
				(jsonb_extract_path(summary.attached, 'spec') - 'harness'::text) ||
				CASE
					WHEN history.sandbox_name <> '' AND history.sandbox_uid <> '' AND
						jsonb_extract_path_text(summary.attached, 'spec', 'harness') IN ('HARNESS_CLAUDE', 'HARNESS_CODEX') THEN
						jsonb_build_object('environmentHosted', jsonb_build_object(
							'environment', jsonb_build_object('name', history.sandbox_name, 'uid', history.sandbox_uid),
							'harness', jsonb_extract_path(summary.attached, 'spec', 'harness')))
					WHEN history.sandbox_name <> '' AND history.sandbox_uid <> '' AND
						jsonb_extract_path_text(summary.attached, 'spec', 'harness') = 'HARNESS_CENTRAL_SAMPLER' THEN
						jsonb_build_object('centralSamplerV1', jsonb_build_object(
							'executorEnvironment', jsonb_build_object('name', history.sandbox_name, 'uid', history.sandbox_uid)))
					ELSE '{}'::jsonb
				END) ||
			CASE
				WHEN history.sandbox_name <> '' AND history.sandbox_uid <> '' AND
					jsonb_extract_path_text(summary.attached, 'spec', 'harness') IN ('HARNESS_CLAUDE', 'HARNESS_CODEX', 'HARNESS_CENTRAL_SAMPLER')
					THEN '{}'::jsonb
				ELSE jsonb_build_object('historicalHarness', jsonb_extract_path(summary.attached, 'spec', 'harness'))
			END
			FROM session_history AS history
			WHERE summary.thread_id=history.id AND jsonb_extract_path(summary.attached, 'spec') ? 'harness'::text`)
		if pgErr, ok := err.(*pq.Error); ok {
			return fmt.Errorf("convert archived summary: %w (detail: %s, where: %s)", err, pgErr.Detail, pgErr.Where)
		}
		return err
	}},
	&goose.GoFunc{RunTx: func(context.Context, *sql.Tx) error {
		return fmt.Errorf("archived harness provenance migration is irreversible")
	}})

func historicalHarness(data []byte) (sessionpb.Harness, []byte, bool, error) {
	var harness sessionpb.Harness
	var rest []byte
	found := false
	for len(data) != 0 {
		number, kind, n := protowire.ConsumeTag(data)
		if n < 0 {
			return 0, nil, false, protowire.ParseError(n)
		}
		length := protowire.ConsumeFieldValue(number, kind, data[n:])
		if length < 0 {
			return 0, nil, false, protowire.ParseError(length)
		}
		if number == 1 {
			if kind != protowire.VarintType {
				return 0, nil, false, fmt.Errorf("historical harness has wrong wire type")
			}
			value, _ := protowire.ConsumeVarint(data[n:])
			harness = sessionpb.Harness(value)
			found = true
		} else {
			rest = append(rest, data[:n+length]...)
		}
		data = data[n+length:]
	}
	return harness, rest, found, nil
}
