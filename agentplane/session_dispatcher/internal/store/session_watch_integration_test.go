package store

import (
	"context"
	"database/sql"
	"testing"
	"time"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
)

func TestSessionWatchVersionsFollowSerializedCommitOrder(t *testing.T) {
	store, _ := testStoreWithURL(t, 3)
	ctx := t.Context()
	ids := []string{uuid.NewString(), uuid.NewString()}
	for _, id := range ids {
		if _, err := createSession(store, ctx, id, "test-watch-version", uuid.NewString(), &sessionpb.SessionSpec{}, nil, nil); err != nil {
			t.Fatal(err)
		}
	}
	cursorRow, err := store.GetSession(ctx, ids[1])
	if err != nil {
		t.Fatal(err)
	}
	cursor := cursorRow.Version
	first, err := store.DB.Conn(ctx)
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.DB.Conn(ctx)
	if err != nil {
		_ = first.Close()
		t.Fatal(err)
	}
	for _, conn := range []*sql.Conn{first, second} {
		conn := conn
		t.Cleanup(func() {
			_, _ = conn.ExecContext(context.Background(), "ROLLBACK")
			_ = conn.Close()
		})
		if _, err := conn.ExecContext(ctx, "BEGIN"); err != nil {
			t.Fatal(err)
		}
	}
	firstPID := postgres.BackendPID(t, ctx, first)
	secondPID := postgres.BackendPID(t, ctx, second)
	if _, err := first.ExecContext(ctx, "UPDATE session SET reason='writer-a' WHERE session_id=$1", ids[0]); err != nil {
		t.Fatal(err)
	}
	if rows, err := store.SessionsAfter(ctx, cursor, ServiceCaller{}); err != nil || len(rows) != 0 {
		t.Fatalf("reader crossed an uncommitted Session version: rows=%v err=%v", rows, err)
	}
	secondCtx, cancelSecond := context.WithTimeout(ctx, 10*time.Second)
	t.Cleanup(cancelSecond)
	secondDone := make(chan error, 1)
	go func() {
		_, err := second.ExecContext(secondCtx, "UPDATE session SET reason='writer-b' WHERE session_id=$1", ids[1])
		secondDone <- err
	}()
	postgres.WaitForBlocker(t, secondCtx, store.DB, secondPID, firstPID)
	if _, err := first.ExecContext(ctx, "COMMIT"); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-secondDone:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("second Session writer remained blocked after the first commit")
	}
	if rows, err := store.SessionsAfter(ctx, cursor, ServiceCaller{}); err != nil || len(rows) != 1 || rows[0].SessionID != ids[0] {
		t.Fatalf("reader after first commit = %+v err=%v, want first Session only", rows, err)
	}
	if _, err := second.ExecContext(ctx, "COMMIT"); err != nil {
		t.Fatal(err)
	}
	rows, err := store.SessionsAfter(ctx, cursor, ServiceCaller{})
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 || rows[0].SessionID != ids[0] || rows[1].SessionID != ids[1] {
		t.Fatalf("resume at lower checkpoint returned %+v, want both committed updates in order", rows)
	}
	if rows[0].Version >= rows[1].Version {
		t.Fatalf("versions %d then %d do not follow the serialized commits", rows[0].Version, rows[1].Version)
	}
}
