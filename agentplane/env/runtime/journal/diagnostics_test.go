package journal

import (
	"context"
	"database/sql"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
	sqlite3 "modernc.org/sqlite/lib"
)

func TestJournalFailureLogsFirstSQLPhase(t *testing.T) {
	for _, phase := range []string{"begin", "commit"} {
		for _, batched := range []bool{false, true} {
			name := phase + "/single"
			if batched {
				name = phase + "/batch"
			}
			t.Run(name, func(t *testing.T) {
				core, observed := observer.New(zap.DebugLevel)
				undo := zap.ReplaceGlobals(zap.New(core))
				t.Cleanup(undo)
				ctx := t.Context()
				journal, _, read := journalWithExternalReader(t)
				if _, err := journal.conn.ExecContext(ctx, "PRAGMA busy_timeout=0"); err != nil {
					t.Fatal(err)
				}
				if phase == "begin" {
					// Upgrade the other connection from SHARED to RESERVED.
					if _, err := read.ExecContext(ctx, "UPDATE checkpoint SET through_cursor=through_cursor"); err != nil {
						t.Fatal(err)
					}
				}
				write := func(ctx context.Context) error {
					_, err := journal.Append(ctx, &eventpb.Event{Observation: &eventpb.Event_HarnessLost{HarnessLost: &eventpb.HarnessLost{}}}, AppendOptions{})
					return err
				}
				var err error
				if batched {
					err = journal.WithBatch(ctx, write)
				} else {
					err = write(ctx)
				}
				if !isStorageError(err) {
					t.Fatalf("write = %v, want storage error", err)
				}
				if err := write(ctx); !errors.Is(err, ErrFailed) {
					t.Fatalf("next write = %v, want failed journal", err)
				}
				entries := observed.FilterMessage("Session journal failed").All()
				if len(entries) != 1 {
					t.Fatalf("failure entries = %d, want exactly one", len(entries))
				}
				fields := entries[0].ContextMap()
				info, err := os.Stat(journal.path)
				if err != nil {
					t.Fatal(err)
				}
				stat := info.Sys().(*syscall.Stat_t)
				for key, want := range map[string]any{
					"phase": phase, "source_id": "test-source", "journal_path": journal.path,
					"pid": int64(os.Getpid()), "sqlite_code": int64(sqlite3.SQLITE_BUSY),
					"open_path_device": uint64(stat.Dev), "open_path_inode": stat.Ino,
					"failure_path_device": uint64(stat.Dev), "failure_path_inode": stat.Ino,
				} {
					if fields[key] != want {
						t.Errorf("field %s = %v, want %v", key, fields[key], want)
					}
				}
				callers := observed.FilterMessage("Session journal failure caller").All()
				if len(callers) != 1 || !strings.Contains(callers[0].ContextMap()["stack"].(string), "TestJournalFailureLogsFirstSQLPhase") {
					t.Fatalf("missing failure caller: %+v", callers)
				}
				if _, ok := fields["stack"]; ok {
					t.Fatal("error-level record includes debug stack")
				}
			})
		}
	}
}

func TestJournalFailureAtInfoOmitsStackAndObservesReplacedPath(t *testing.T) {
	core, observed := observer.New(zap.InfoLevel)
	undo := zap.ReplaceGlobals(zap.New(core))
	t.Cleanup(undo)
	journal := openTestJournal(t)
	// Keep the original inode alive; replacing its pathname must not make a
	// diagnostic claim that SQLite's descriptor also changed.
	if err := os.Rename(journal.path, filepath.Join(filepath.Dir(journal.path), "old.sqlite")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(journal.path, nil, 0600); err != nil {
		t.Fatal(err)
	}
	err := journal.write(t.Context(), func(*journalBatch) error {
		return storageError("injected operation", sql.ErrConnDone)
	})
	if !isStorageError(err) {
		t.Fatalf("write = %v, want storage error", err)
	}
	entries := observed.FilterMessage("Session journal failed").All()
	if len(entries) != 1 {
		t.Fatalf("failure entries = %d, want one", len(entries))
	}
	fields := entries[0].ContextMap()
	if fields["phase"] != "operation" || fields["open_path_inode"] == fields["failure_path_inode"] {
		t.Fatalf("incorrect phase or replaced-path identity: %+v", fields)
	}
	if observed.FilterMessage("Session journal failure caller").Len() != 0 {
		t.Fatal("info threshold emitted debug stack")
	}
}
