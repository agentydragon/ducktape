package native

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const largeStdinWrite = 2 << 20

func TestBlockedStdinWriteCanCancelAndResume(t *testing.T) {
	limits := DefaultLimits()
	limits.MaxStdinQueueBytesPerProcess = largeStdinWrite
	server, err := NewWithLimits(limits)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = server.Close() })
	session, err := server.OpenSession("")
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	cwd := (&url.URL{Scheme: "file", Path: dir}).String()
	payload := bytes.Repeat([]byte("a"), largeStdinWrite)
	script := `
		while [ ! -f drain ]; do sleep 0.01; done
		head -c "$1" | sha256sum
		IFS= read -r line
		printf 'next:%s\n' "$line"
	`
	if err := session.Start(StartRequest{
		ProcessID: "blocked", Argv: []string{"/bin/sh", "-c", script, "stdin", fmt.Sprint(len(payload))},
		Cwd: cwd, PipeStdin: true,
	}); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if status, err := testStdinWrite(t, ctx, session, "initial", payload); err != nil || status != "accepted" {
		t.Fatalf("initial write = %q, %v", status, err)
	}
	awaitStdinBackpressure(t, ctx, session, len(payload))

	attachment, detach := context.WithTimeout(ctx, 100*time.Millisecond)
	defer detach()
	if status, err := testStdinWrite(t, attachment, session, "later", []byte("canceled\n")); !errors.Is(err, context.DeadlineExceeded) || status != "" {
		t.Fatalf("blocked write = %q, %v", status, err)
	}
	if reserved := server.writeCount.Load(); reserved != 1 {
		t.Fatalf("retained write IDs after cancellation = %d, want 1", reserved)
	}

	server.Detach(session.ID())
	resumed, err := server.OpenSession(session.ID())
	if err != nil {
		t.Fatal(err)
	}
	if status, err := testStdinWrite(t, ctx, resumed, "initial", []byte("duplicate\n")); err != nil || status != "accepted" {
		t.Fatalf("retry of accepted write = %q, %v", status, err)
	}
	if err := os.WriteFile(filepath.Join(dir, "drain"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	if status, err := testStdinWrite(t, ctx, resumed, "later", []byte("resumed\n")); err != nil || status != "accepted" {
		t.Fatalf("retry of canceled write = %q, %v", status, err)
	}

	var output strings.Builder
	var after *uint64
	for {
		if err := ctx.Err(); err != nil {
			t.Fatal(err)
		}
		read, err := resumed.Read(ctx, "blocked", after, 0, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		for _, chunk := range read.Chunks {
			output.Write(chunk.Data)
		}
		seq := read.NextSeq - 1
		after = &seq
		if read.Closed {
			if read.ExitCode == nil || *read.ExitCode != 0 {
				t.Fatalf("child exit = %v, output = %q", read.ExitCode, output.String())
			}
			break
		}
	}
	want := fmt.Sprintf("%x  -\nnext:resumed\n", sha256.Sum256(payload))
	if got := output.String(); got != want {
		t.Fatalf("child output = %q, want %q", got, want)
	}
}

func TestCloseAbortsBlockedStdin(t *testing.T) {
	for _, tc := range []struct {
		name string
		tty  bool
		args []string
	}{
		{"pipe", false, []string{"/bin/sh", "-c", "printf ready; exec sleep 30"}},
		{"pty", true, []string{"/bin/sh", "-c", "stty raw -echo && printf ready && exec sleep 30"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := New()
			t.Cleanup(func() { _ = server.Close() })
			session, err := server.OpenSession("")
			if err != nil {
				t.Fatal(err)
			}
			cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
			if err := session.Start(StartRequest{
				ProcessID: "blocked", Argv: tc.args, Cwd: cwd, TTY: tc.tty, PipeStdin: !tc.tty,
			}); err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			var output strings.Builder
			var after *uint64
			for output.String() != "ready" {
				if err := ctx.Err(); err != nil {
					t.Fatalf("child did not get ready: %v, output = %q", err, output.String())
				}
				read, err := session.Read(ctx, "blocked", after, 0, time.Second)
				if err != nil {
					t.Fatal(err)
				}
				for _, chunk := range read.Chunks {
					output.Write(chunk.Data)
				}
				if read.Closed {
					t.Fatalf("child exited before reporting readiness, output = %q", output.String())
				}
				seq := read.NextSeq - 1
				after = &seq
			}
			payload := make([]byte, largeStdinWrite)
			if status, err := testStdinWrite(t, ctx, session, "initial", payload); err != nil || status != "accepted" {
				t.Fatalf("write = %q, %v", status, err)
			}
			p := awaitStdinBackpressure(t, ctx, session, len(payload))
			if status, err := testStdinWrite(t, ctx, session, "queued", []byte("waiting")); err != nil || status != "accepted" {
				t.Fatalf("queued write = %q, %v", status, err)
			}

			closed := make(chan struct{})
			go func() {
				_ = server.Close()
				close(closed)
			}()
			select {
			case <-closed:
			case <-ctx.Done():
				t.Fatal("server close did not return while child stdin was blocked")
			}
			select {
			case <-p.stdinDone:
			default:
				t.Fatal("server close returned before the stdin worker stopped")
			}
			if remaining := server.stdinBytes.Load(); remaining != 0 {
				t.Fatalf("stdin bytes retained after close = %d", remaining)
			}
			if remaining := server.writeCount.Load(); remaining != 0 {
				t.Fatalf("write IDs retained after close = %d", remaining)
			}
		})
	}
}

func testStdinWrite(t *testing.T, ctx context.Context, s *Session, id string, data []byte) (string, error) {
	t.Helper()
	type result struct {
		status string
		err    error
	}
	done := make(chan result, 1)
	go func() {
		status, err := s.WriteContext(ctx, "blocked", id, data)
		done <- result{status, err}
	}()
	select {
	case r := <-done:
		return r.status, r.err
	case <-time.After(3 * time.Second):
		t.Fatal("write RPC did not return")
		return "", nil
	}
}

func awaitStdinBackpressure(t *testing.T, ctx context.Context, s *Session, bytes int) *process {
	t.Helper()
	p, err := s.get("blocked")
	if err != nil {
		t.Fatal(err)
	}
	for {
		p.writeMu.Lock()
		blocked := len(p.stdinQueue) == 0 && p.stdinBytes == bytes
		changed := p.stdinChanged
		p.writeMu.Unlock()
		if blocked {
			return p
		}
		select {
		case <-changed:
		case <-ctx.Done():
			t.Fatal("stdin worker did not dequeue the large write")
		}
	}
}

func TestSignalAndTerminateIgnoreMissingOrExitedProcess(t *testing.T) {
	server := New()
	t.Cleanup(func() { _ = server.Close() })
	session, err := server.OpenSession("")
	if err != nil {
		t.Fatal(err)
	}
	cwd := (&url.URL{Scheme: "file", Path: t.TempDir()}).String()
	if err := session.Start(StartRequest{ProcessID: "exited", Argv: []string{"/bin/true"}, Cwd: cwd}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	read, err := session.Read(ctx, "exited", nil, 0, 5*time.Second)
	if err != nil || !read.Exited {
		t.Fatalf("expected recorded exit, got %+v, %v", read, err)
	}
	for _, id := range []string{"missing", "exited"} {
		if err := session.Signal(id); err != nil {
			t.Fatalf("signal %s: %v", id, err)
		}
		if running, err := session.Terminate(id); err != nil || running {
			t.Fatalf("terminate %s = %v, %v", id, running, err)
		}
	}
}
