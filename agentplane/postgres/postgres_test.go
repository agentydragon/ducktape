package postgres_test

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	fixture "github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var database *fixture.Database

func TestMain(m *testing.M) {
	os.Exit(fixture.RunIntegrationTests(m.Run, func(ctx context.Context, db *sql.DB) error {
		_, err := db.ExecContext(ctx, "CREATE TABLE transaction_probe (id integer PRIMARY KEY)")
		return err
	}, &database))
}

func TestNamedTransaction(t *testing.T) {
	db := fixture.Open(t, fixture.URLWithApplicationName(t, database.URL, "fixture").String())
	db.SetMaxOpenConns(1)
	var log bytes.Buffer
	old := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&log, nil)))
	t.Cleanup(func() { slog.SetDefault(old) })

	ctx := t.Context()
	rollback := errors.New("request rollback")
	var pid int
	err := postgres.NamedTransaction(ctx, db, sql.TxOptions{}, "fixture.rollback", func(tx *sql.Tx) error {
		var name string
		if err := tx.QueryRowContext(ctx, "SELECT pg_backend_pid(), current_setting('application_name')").Scan(&pid, &name); err != nil {
			return err
		}
		if name != "fixture.rollback" {
			t.Errorf("name in transaction = %q, want fixture.rollback", name)
		}
		if _, err := tx.ExecContext(ctx, "INSERT INTO transaction_probe VALUES (1)"); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, "SELECT pg_sleep(1.01)"); err != nil {
			return err
		}
		return rollback
	})
	if !errors.Is(err, rollback) {
		t.Fatalf("NamedTransaction error = %v, want %v", err, rollback)
	}
	for _, field := range []string{"operation=fixture.rollback", "pg_backend_pid=" + strconv.Itoa(pid), "started_at=", "ended_at=", "succeeded=false"} {
		if !strings.Contains(log.String(), field) {
			t.Errorf("slow transaction log missing %q: %s", field, &log)
		}
	}

	var reusedPID, count int
	var name string
	if err := db.QueryRowContext(ctx, `
		SELECT pg_backend_pid(), current_setting('application_name'), (SELECT count(*) FROM transaction_probe)
	`).Scan(&reusedPID, &name, &count); err != nil {
		t.Fatal(err)
	}
	if reusedPID != pid || name != "fixture" || count != 0 {
		t.Fatalf("after rollback: pid=%d, name=%q, rows=%d; want pid=%d, name=fixture, rows=0", reusedPID, name, count, pid)
	}
}
