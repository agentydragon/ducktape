package journal

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

func admitConfiguration(t *testing.T, j *Journal) {
	t.Helper()
	command := &commandpb.Command{CommandId: "change", Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: "new"}}}
	if _, _, err := j.Admit(t.Context(), command, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := j.DispatchPlanned(t.Context(), command.CommandId); err != nil {
		t.Fatal(err)
	}
}

func appendConfiguration(ctx context.Context, j *Journal) error {
	_, err := j.Append(ctx, &eventpb.Event{Observation: &eventpb.Event_ModelChanged{ModelChanged: &eventpb.ModelChanged{CommandId: "change", PreviousModel: "old", Model: "new"}}}, AppendOptions{TerminalCommandIDs: []string{"change"}})
	return err
}

func assertConfigurationOutcome(t *testing.T, j *Journal, committed bool) {
	t.Helper()
	receipt, err := j.GetCommand(t.Context(), "change")
	if err != nil {
		t.Fatal(err)
	}
	if !receipt.DispatchPlanned || (receipt.TerminalCursor != nil) != committed {
		t.Fatalf("receipt: %+v", receipt)
	}
	checkpoint := j.RecoveryState()
	if (checkpoint.Model != nil) != committed || committed && *checkpoint.Model != "new" {
		t.Fatalf("configuration: %+v", checkpoint)
	}
	entries, err := j.Since(t.Context(), 1, 10)
	if err != nil {
		t.Fatal(err)
	}
	if committed {
		if len(entries) != 1 || entries[0].Event.GetModelChanged().Model != *checkpoint.Model || entries[0].Cursor != *receipt.TerminalCursor {
			t.Fatalf("outcome mismatch: %v", entries)
		}
	} else if len(entries) != 0 {
		t.Fatalf("rolled back outcome retained: %v", entries)
	}
}

func TestConfigurationWriteFailuresAreAtomic(t *testing.T) {
	for _, table := range []string{"checkpoint", "command"} {
		t.Run(table, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "journal.sqlite")
			j, err := Open(t.Context(), path, "source")
			if err != nil {
				t.Fatal(err)
			}
			admitConfiguration(t, j)
			// Fail after event insertion, or after both event and checkpoint writes.
			if _, err := j.conn.ExecContext(t.Context(), "CREATE TEMP TRIGGER fail_configuration BEFORE UPDATE ON "+table+" BEGIN SELECT RAISE(ABORT, 'injected'); END"); err != nil {
				t.Fatal(err)
			}
			if err := appendConfiguration(t.Context(), j); !isStorageError(err) {
				t.Fatalf("write error: %v", err)
			}
			if state := j.RecoveryState(); state.Model != nil || state.ThroughCursor != 1 {
				t.Fatalf("failed write published: %+v", state)
			}
			if err := j.Close(); err != nil {
				t.Fatal(err)
			}
			reopened, err := Open(t.Context(), path, "source")
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = reopened.Close() })
			assertConfigurationOutcome(t, reopened, false)
		})
	}
}

func TestConfigurationProcessCrash(t *testing.T) {
	if path := os.Getenv("SESSION_CONFIGURATION_CRASH_PATH"); path != "" {
		j, err := Open(t.Context(), path, "source")
		if err != nil {
			t.Fatal(err)
		}
		admitConfiguration(t, j)
		if err := j.WithBatch(t.Context(), func(ctx context.Context) error {
			if err := appendConfiguration(ctx, j); err != nil {
				return err
			}
			if os.Getenv("SESSION_CONFIGURATION_COMMIT") == "yes" {
				if err := j.CommitBatch(ctx); err != nil {
					return err
				}
			}
			os.Exit(17) // No Close or deferred cleanup: recover SQLite's actual crash boundary.
			return nil
		}); err != nil {
			t.Fatal(err)
		}
		t.Fatal("crash child returned")
	}
	for _, commit := range []string{"no", "yes"} {
		t.Run(commit, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "journal.sqlite")
			child := exec.Command(os.Args[0], "-test.run=^TestConfigurationProcessCrash$")
			child.Env = append(os.Environ(), "SESSION_CONFIGURATION_CRASH_PATH="+path, "SESSION_CONFIGURATION_COMMIT="+commit)
			output, err := child.CombinedOutput()
			exit, ok := err.(*exec.ExitError)
			if !ok || exit.ExitCode() != 17 {
				t.Fatalf("crash child: %v\n%s", err, output)
			}
			j, err := Open(t.Context(), path, "source")
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = j.Close() })
			assertConfigurationOutcome(t, j, commit == "yes")
		})
	}
}
