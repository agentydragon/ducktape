// Package migrations owns the guarded Goose handoff for the Dispatcher database.
package migrations

import (
	"context"
	"database/sql"
	"embed"
	"io/fs"

	"github.com/agentydragon/ducktape/agentplane/postgres/goosehandoff"
)

//go:embed dispatcher/*.sql
var scripts embed.FS

var dispatcherSchema = goosehandoff.Schema{
	Head: "0007_drop_opening_attached", AlembicTable: "alembic_version", LockID: 0x4453_5043,
	GooseTable: "runner_gateway_dispatcher_goose_version",
	Columns: map[string]map[string]goosehandoff.Column{
		"dispatch": {
			"position": goosehandoff.Identity("int8"), "command_id": goosehandoff.Required("varchar"),
			"session_id": goosehandoff.Required("varchar"), "sandbox_name": goosehandoff.Required("varchar"),
			"sandbox_uid": goosehandoff.Required("uuid"), "operation": goosehandoff.Required("varchar"),
			"command": goosehandoff.Required("bytea"), "state": goosehandoff.Required("varchar"),
			"reason": goosehandoff.Nullable("varchar"), "admission": goosehandoff.Nullable("bytea"),
			"created_at": goosehandoff.Required("timestamptz"), "updated_at": goosehandoff.Required("timestamptz"),
			"wake_eligible": goosehandoff.Required("bool"), "wake_from_incarnation": goosehandoff.Nullable("varchar"),
			"wake_caller_namespace": goosehandoff.Nullable("varchar"), "wake_caller_name": goosehandoff.Nullable("varchar"),
			"wake_requested_at": goosehandoff.Nullable("timestamptz"),
		},
		"session_opening": {
			"session_id": goosehandoff.Required("varchar"), "sandbox_name": goosehandoff.Required("varchar"),
			"sandbox_uid": goosehandoff.Required("uuid"), "spec": goosehandoff.Required("bytea"),
			"setup_script": goosehandoff.Nullable("varchar"), "state": goosehandoff.Required("varchar"),
			"reason":     goosehandoff.Nullable("varchar"),
			"created_at": goosehandoff.Required("timestamptz"), "updated_at": goosehandoff.Required("timestamptz"),
			"wake_eligible": goosehandoff.Required("bool"), "wake_from_incarnation": goosehandoff.Nullable("varchar"),
			"wake_caller_namespace": goosehandoff.Nullable("varchar"), "wake_caller_name": goosehandoff.Nullable("varchar"),
			"wake_requested_at": goosehandoff.Nullable("timestamptz"),
		},
		"sandbox_ownership": {
			"sandbox_uid": goosehandoff.Required("uuid"), "generation": goosehandoff.Required("int8"),
			"lease_uid": goosehandoff.Required("varchar"), "lease_transitions": goosehandoff.Required("int4"),
			"holder_identity": goosehandoff.Required("varchar"), "updated_at": goosehandoff.Required("timestamptz"),
		},
		"wake_attempt": {
			"caller_namespace": goosehandoff.Required("varchar"), "caller_name": goosehandoff.Required("varchar"),
			"sandbox_uid": goosehandoff.Required("uuid"), "wake_generation": goosehandoff.Required("int8"),
			"attempted_at": goosehandoff.Required("timestamptz"),
		},
	},
	Indexes: map[string]map[string]goosehandoff.Index{
		"dispatch": {
			"dispatch_pkey": goosehandoff.Unique("position"), "dispatch_command_id_key": goosehandoff.Unique("command_id"),
			"ix_dispatch_session_state":     goosehandoff.NonUnique("session_id,state"),
			"ix_dispatch_sandbox_uid_state": goosehandoff.NonUnique("sandbox_uid,state"),
			"ix_dispatch_sandbox_name":      goosehandoff.NonUnique("sandbox_name"),
		},
		"session_opening": {
			"session_opening_pkey":           goosehandoff.Unique("session_id"),
			"ix_session_opening_sandbox_uid": goosehandoff.NonUnique("sandbox_uid"),
		},
		"sandbox_ownership": {"sandbox_ownership_pkey": goosehandoff.Unique("sandbox_uid")},
		"wake_attempt": {
			"wake_attempt_pkey":           goosehandoff.Unique("caller_namespace,caller_name,sandbox_uid,wake_generation"),
			"ix_wake_attempt_caller_time": goosehandoff.NonUnique("caller_namespace,caller_name,attempted_at"),
		},
	},
}

// Up applies authored SQL with Goose. See goosehandoff.Up.
func Up(ctx context.Context, db *sql.DB) error {
	files, err := fs.Sub(scripts, "dispatcher")
	if err != nil {
		return err
	}
	return goosehandoff.Up(ctx, db, files, dispatcherSchema)
}
