package postgres

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestRunImageLoaderCancellationKillsChildProcess(t *testing.T) {
	pidFile := filepath.Join(t.TempDir(), "child.pid")
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	finished := make(chan error, 1)
	go func() {
		finished <- runImageLoader(
			ctx,
			"image:test",
			"/bin/sh",
			"-c",
			`sleep 30 & echo $! > "$1.tmp" && mv "$1.tmp" "$1"; wait`,
			"sh",
			pidFile,
		)
	}()

	var childPID int
	deadline := time.NewTimer(5 * time.Second)
	ticker := time.NewTicker(10 * time.Millisecond)
	defer deadline.Stop()
	defer ticker.Stop()
	for childPID == 0 {
		select {
		case <-ticker.C:
			contents, err := os.ReadFile(pidFile)
			if err == nil {
				childPID, err = strconv.Atoi(strings.TrimSpace(string(contents)))
				if err != nil {
					t.Fatal(err)
				}
			} else if !errors.Is(err, os.ErrNotExist) {
				t.Fatal(err)
			}
		case <-deadline.C:
			t.Fatal("image loader did not start its child process")
		}
	}

	cancel()
	select {
	case err := <-finished:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("runImageLoader error = %v, want context.Canceled", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("image loader did not stop after cancellation")
	}

	deadline.Reset(5 * time.Second)
	for {
		state, err := processState(childPID)
		if err != nil {
			t.Fatal(err)
		}
		if state == "" || state == "Z" || state == "X" {
			return
		}
		select {
		case <-ticker.C:
		case <-deadline.C:
			t.Fatalf("child process %d remained in state %q after cancellation", childPID, state)
		}
	}
}

func processState(pid int) (string, error) {
	contents, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "stat"))
	if errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	_, fields, ok := strings.Cut(string(contents), ") ")
	if !ok {
		return "", errors.New("malformed process stat")
	}
	state, _, ok := strings.Cut(fields, " ")
	if !ok {
		return "", errors.New("process stat is missing state")
	}
	return state, nil
}
