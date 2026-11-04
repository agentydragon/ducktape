package migrations

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/pressly/goose/v3"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// Migration 8 left an older centralSampler property in some derived JSON summaries.
// Keep the historical service in the authoritative binary feed; remove the obsolete
// property only after proving the same fact is retained there.
var repairLegacySamplerSummary = goose.NewGoMigration(10,
	&goose.GoFunc{RunTx: func(ctx context.Context, tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, `SELECT summary.thread_id, history.feed_state,
			summary.attached->'spec'->'centralSampler', summary.attached->'spec'->'central_sampler'
			FROM thread_history_summary summary LEFT JOIN session_history history ON history.id=summary.thread_id
			WHERE summary.attached->'spec' ?| array['centralSampler','central_sampler']
			FOR UPDATE OF summary`)
		if err != nil {
			return err
		}
		var ids []string
		for rows.Next() {
			var id string
			var rawFeed, camel, snake []byte
			if err := rows.Scan(&id, &rawFeed, &camel, &snake); err != nil {
				rows.Close()
				return err
			}
			if len(camel) != 0 && len(snake) != 0 {
				rows.Close()
				return fmt.Errorf("archived summary %s has conflicting sampler fields", id)
			}
			config := camel
			if len(config) == 0 {
				config = snake
			}
			var object map[string]json.RawMessage
			if err := json.Unmarshal(config, &object); err != nil || object == nil || len(object) > 1 {
				rows.Close()
				return fmt.Errorf("archived summary %s has invalid sampler configuration", id)
			}
			var service string
			if raw, ok := object["service"]; ok {
				if err := json.Unmarshal(raw, &service); err != nil {
					rows.Close()
					return fmt.Errorf("archived summary %s has invalid sampler service", id)
				}
			} else if len(object) != 0 {
				rows.Close()
				return fmt.Errorf("archived summary %s has unknown sampler configuration", id)
			}
			feed := &historypb.SessionFeedState{}
			if err := proto.Unmarshal(rawFeed, feed); err != nil || feed.GetAttached().GetSpec() == nil {
				rows.Close()
				return fmt.Errorf("archived summary %s lacks a retained feed", id)
			}
			retained, found, err := historicalSamplerService(feed.GetAttached().GetSpec().ProtoReflect().GetUnknown())
			if err != nil || !found || retained != service {
				rows.Close()
				return fmt.Errorf("archived summary %s sampler configuration is not preserved in its feed", id)
			}
			ids = append(ids, id)
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		if err := rows.Close(); err != nil {
			return err
		}
		for _, id := range ids {
			if _, err := tx.ExecContext(ctx, `UPDATE thread_history_summary SET attached=jsonb_set(attached, '{spec}',
				(attached->'spec') - 'centralSampler' - 'central_sampler') WHERE thread_id=$1`, id); err != nil {
				return err
			}
		}
		return nil
	}},
	&goose.GoFunc{RunTx: func(context.Context, *sql.Tx) error {
		return fmt.Errorf("archived summary repair is irreversible")
	}})

func historicalSamplerService(data []byte) (string, bool, error) {
	var config []byte
	found := false
	for len(data) > 0 {
		number, kind, n := protowire.ConsumeTag(data)
		if n < 0 {
			return "", false, protowire.ParseError(n)
		}
		length := protowire.ConsumeFieldValue(number, kind, data[n:])
		if length < 0 {
			return "", false, protowire.ParseError(length)
		}
		if number == 6 {
			if kind != protowire.BytesType {
				return "", false, fmt.Errorf("historical sampler configuration has wrong wire type")
			}
			config, _ = protowire.ConsumeBytes(data[n:])
			found = true
		}
		data = data[n+length:]
	}
	var service string
	for len(config) > 0 {
		number, kind, n := protowire.ConsumeTag(config)
		if n < 0 {
			return "", false, protowire.ParseError(n)
		}
		length := protowire.ConsumeFieldValue(number, kind, config[n:])
		if length < 0 {
			return "", false, protowire.ParseError(length)
		}
		if number == 1 {
			if kind != protowire.BytesType {
				return "", false, fmt.Errorf("historical sampler service has wrong wire type")
			}
			service, _ = protowire.ConsumeString(config[n:])
		}
		config = config[n+length:]
	}
	return service, found, nil
}
