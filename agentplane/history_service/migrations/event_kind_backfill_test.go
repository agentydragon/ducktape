package migrations

import (
	"context"
	"database/sql"
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

func TestBackfillSessionEventKindAndRequireIt(t *testing.T) {
	db, err := sql.Open("postgres", migrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	ctx := t.Context()
	const rowCount = 514
	const sessionID = "00000000-0000-0000-0000-000000000001"
	if _, err := db.ExecContext(ctx, "INSERT INTO session_history (id, last_cursor) VALUES ($1, $2)", sessionID, rowCount); err != nil {
		t.Fatal(err)
	}
	knownPayload, err := proto.MarshalOptions{Deterministic: true}.Marshal(&eventlogpb.EventEntry{
		Event: &eventpb.Event{Observation: &eventpb.Event_HarnessStarted{HarnessStarted: &eventpb.HarnessStarted{}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	noObservationPayload, err := proto.MarshalOptions{Deterministic: true}.Marshal(&eventlogpb.EventEntry{})
	if err != nil {
		t.Fatal(err)
	}
	unknownEvent := &eventpb.Event{}
	unknownBytes := protowire.AppendTag(nil, 100, protowire.VarintType)
	unknownEvent.ProtoReflect().SetUnknown(protowire.AppendVarint(unknownBytes, 1))
	unknownPayload, err := proto.MarshalOptions{Deterministic: true}.Marshal(&eventlogpb.EventEntry{Event: unknownEvent})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `INSERT INTO session_event (session_id, cursor, payload)
		SELECT $1, cursor, $2 FROM generate_series(1, $3) AS rows(cursor)`, sessionID, knownPayload, rowCount); err != nil {
		t.Fatal(err)
	}
	for _, update := range []struct {
		cursor  int
		payload []byte
		kind    *int
	}{
		{cursor: 2, payload: unknownPayload},
		{cursor: 3, payload: noObservationPayload},
		{cursor: 512, payload: knownPayload, kind: pointer(99)},
		{cursor: rowCount, payload: []byte{0xff}},
	} {
		if update.kind == nil {
			if _, err := db.ExecContext(ctx, "UPDATE session_event SET payload = $3 WHERE session_id = $1 AND cursor = $2", sessionID, update.cursor, update.payload); err != nil {
				t.Fatal(err)
			}
		} else if _, err := db.ExecContext(ctx, "UPDATE session_event SET payload = $3, kind = $4 WHERE session_id = $1 AND cursor = $2",
			sessionID, update.cursor, update.payload, *update.kind); err != nil {
			t.Fatal(err)
		}
	}

	provider, err := newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.Up(ctx); err == nil {
		t.Fatal("migration accepted malformed protobuf payload")
	}
	assertPartialKinds(t, ctx, db, sessionID, rowCount)
	if _, err := db.ExecContext(ctx, "UPDATE session_event SET payload = $3 WHERE session_id = $1 AND cursor = $2", sessionID, rowCount, knownPayload); err != nil {
		t.Fatal(err)
	}
	provider, err = newProvider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.Up(ctx); err != nil {
		t.Fatal(err)
	}
	want := make([]int64, rowCount)
	for cursor := range want {
		want[cursor] = 4
	}
	want[1], want[2], want[511] = 0, 0, 99
	assertKinds(t, ctx, db, sessionID, want)
	assertKindNullable(t, ctx, db, "NO")
	if _, err := db.ExecContext(ctx, `INSERT INTO session_event (session_id, cursor, payload)
		VALUES ($1, $2, $3)`, sessionID, rowCount+1, []byte{}); err == nil {
		t.Fatal("insert without kind succeeded after migration")
	}

	if _, err := provider.DownTo(ctx, 3); err != nil {
		t.Fatal(err)
	}
	assertKinds(t, ctx, db, sessionID, want)
	assertKindNullable(t, ctx, db, "YES")
	if _, err := db.ExecContext(ctx, `INSERT INTO session_event (session_id, cursor, payload)
		VALUES ($1, $2, $3)`, sessionID, rowCount+1, []byte{}); err != nil {
		t.Fatalf("insert without kind after rollback: %v", err)
	}
}

func assertKinds(t *testing.T, ctx context.Context, db *sql.DB, sessionID string, want []int64) {
	t.Helper()
	got := readKinds(t, ctx, db, sessionID)
	if len(got) != len(want) {
		t.Fatalf("got %d kinds, want %d", len(got), len(want))
	}
	for cursor, kind := range got {
		if !kind.Valid || kind.Int64 != want[cursor] {
			t.Errorf("cursor %d kind = %v, want %d", cursor+1, kind, want[cursor])
		}
	}
}

func assertPartialKinds(t *testing.T, ctx context.Context, db *sql.DB, sessionID string, rowCount int) {
	t.Helper()
	got := readKinds(t, ctx, db, sessionID)
	if len(got) != rowCount {
		t.Fatalf("got %d kinds, want %d", len(got), rowCount)
	}
	// The first batch holds cursors 1-512 and commits; the second holds 513 and the malformed 514.
	for cursor, kind := range got[:eventKindBatchSize] {
		want := int64(4)
		if cursor == 1 || cursor == 2 {
			want = 0
		} else if cursor == 511 {
			want = 99
		}
		if !kind.Valid || kind.Int64 != want {
			t.Errorf("cursor %d kind = %v, want %d before retry", cursor+1, kind, want)
		}
	}
	for cursor, kind := range got[eventKindBatchSize:] {
		if kind.Valid {
			t.Errorf("cursor %d kind = %v, want NULL after failed batch", eventKindBatchSize+cursor+1, kind)
		}
	}
}

func readKinds(t *testing.T, ctx context.Context, db *sql.DB, sessionID string) []sql.NullInt64 {
	t.Helper()
	rows, err := db.QueryContext(ctx, "SELECT kind FROM session_event WHERE session_id = $1 ORDER BY cursor", sessionID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var got []sql.NullInt64
	for rows.Next() {
		var kind sql.NullInt64
		if err := rows.Scan(&kind); err != nil {
			t.Fatal(err)
		}
		got = append(got, kind)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return got
}

func assertKindNullable(t *testing.T, ctx context.Context, db *sql.DB, want string) {
	t.Helper()
	var nullable string
	if err := db.QueryRowContext(ctx, `SELECT is_nullable FROM information_schema.columns
		WHERE table_schema = 'public' AND table_name = 'session_event' AND column_name = 'kind'`).Scan(&nullable); err != nil {
		t.Fatal(err)
	}
	if nullable != want {
		t.Fatalf("kind nullable = %q, want %q", nullable, want)
	}
}

func pointer(value int) *int { return &value }
