// Package migrations owns the guarded Goose handoff for the egress decision-history database.
package migrations

import (
	"context"
	"database/sql"
	"embed"
	"io/fs"

	"github.com/agentydragon/ducktape/agentplane/postgres/goosehandoff"
)

//go:embed history/*.sql
var scripts embed.FS

var historySchema = goosehandoff.Schema{
	Head: "0002_service_account_subject", AlembicTable: "egress_alembic_version", LockID: 0x4547_5244, // "EGRD"
	GooseTable: "egress_goose_version",
	Columns: map[string]map[string]goosehandoff.Column{
		"egress_decision": {
			"event_id": goosehandoff.Required("uuid"), "decided_at": goosehandoff.Required("timestamptz"),
			"ingested_at": goosehandoff.Required("timestamptz"), "producer_id": goosehandoff.Required("uuid"),
			"subject_namespace": goosehandoff.Nullable("text"), "subject_name": goosehandoff.Nullable("text"),
			"source_pod_uid": goosehandoff.Nullable("text"), "connection_id": goosehandoff.Required("text"),
			"phase": goosehandoff.Required("text"), "method": goosehandoff.Required("text"),
			"host": goosehandoff.Required("text"), "port": goosehandoff.Required("int4"),
			"outcome": goosehandoff.Required("text"), "reason": goosehandoff.Nullable("text"),
			"binding": goosehandoff.Nullable("text"), "policy": goosehandoff.Nullable("text"),
			"rule": goosehandoff.Nullable("int4"), "substituted": goosehandoff.Required("bool"),
			"address": goosehandoff.Nullable("inet"),
		},
	},
	Indexes: map[string]map[string]goosehandoff.Index{
		"egress_decision": {
			"egress_decision_pkey":           goosehandoff.Unique("event_id"),
			"egress_decision_subject_recent": goosehandoff.NonUnique("subject_namespace,subject_name,decided_at,event_id"),
			"egress_decision_denials_recent": goosehandoff.NonUnique("outcome,decided_at,event_id"),
			"egress_decision_retention":      goosehandoff.NonUnique("decided_at,event_id"),
		},
	},
	Checks: map[string][]string{"egress_decision": {"egress_decision_subject_whole"}},
}

// Up applies authored SQL with Goose. See goosehandoff.Up.
func Up(ctx context.Context, db *sql.DB) error {
	files, err := fs.Sub(scripts, "history")
	if err != nil {
		return err
	}
	return goosehandoff.Up(ctx, db, files, historySchema)
}
