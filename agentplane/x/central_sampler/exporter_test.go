package centralsampler

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/runner_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type fixtureHistory struct {
	ingresspb.UnimplementedRunnerIngressServer
	mu                  sync.Mutex
	entries             map[uint64]*eventlogpb.EventEntry
	cursor              uint64
	loseAcknowledgement bool
	onEntry             func() error
	snapshots           []*runnerpb.Attached
	received            chan uint64
}

func (h *fixtureHistory) Push(stream grpc.BidiStreamingServer[ingresspb.PushFrame, ingresspb.Acknowledgement]) error {
	md, _ := metadata.FromIncomingContext(stream.Context())
	if values := md.Get("authorization"); len(values) != 1 || values[0] != "Bearer test-history-token" {
		return status.Error(codes.Unauthenticated, "fixture bearer refused")
	}
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	announcement := first.GetSamplerAnnounce()
	if announcement == nil {
		return status.Error(codes.InvalidArgument, "missing announcement")
	}
	h.mu.Lock()
	cursor := h.cursor
	h.mu.Unlock()
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: cursor}); err != nil {
		return err
	}
	for {
		frame, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			h.mu.Lock()
			h.snapshots = append(h.snapshots, proto.Clone(announcement.Session).(*runnerpb.Attached))
			h.mu.Unlock()
			return nil
		}
		if err != nil {
			return err
		}
		if entry := frame.GetEntry(); entry != nil {
			h.mu.Lock()
			if entry.GetCursor() <= h.cursor {
				previous := h.entries[entry.GetCursor()]
				h.mu.Unlock()
				if !proto.Equal(previous, entry) {
					return status.Error(codes.DataLoss, "replay changed")
				}
				continue
			}
			if entry.GetCursor() != h.cursor+1 {
				h.mu.Unlock()
				return status.Error(codes.DataLoss, "cursor gap")
			}
			h.entries[entry.GetCursor()] = proto.Clone(entry).(*eventlogpb.EventEntry)
			h.cursor = entry.GetCursor()
			hook := h.onEntry
			h.onEntry = nil
			h.mu.Unlock()
			if hook != nil {
				if err := hook(); err != nil {
					return err
				}
			}
			select {
			case h.received <- entry.GetCursor():
			case <-stream.Context().Done():
				return stream.Context().Err()
			}
		}
		if frame.GetHeartbeat() != nil {
			h.mu.Lock()
			cursor := h.cursor
			lose := h.loseAcknowledgement
			h.loseAcknowledgement = false
			h.mu.Unlock()
			if lose {
				return status.Error(codes.Unavailable, "fixture lost acknowledgement")
			}
			if err := stream.Send(&ingresspb.Acknowledgement{Cursor: cursor}); err != nil {
				return err
			}
		}
	}
}

func startFixtureHistory(t *testing.T, h *fixtureHistory) (string, string) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	ingresspb.RegisterRunnerIngressServer(server, h)
	go server.Serve(listener)
	t.Cleanup(func() { server.Stop(); listener.Close() })
	tokenFile := filepath.Join(t.TempDir(), "history-token")
	if err := os.WriteFile(tokenFile, []byte("test-history-token"), 0600); err != nil {
		t.Fatal(err)
	}
	return listener.Addr().String(), tokenFile
}

func exportFixtureSession(t *testing.T, store *Store) *Session {
	t.Helper()
	session, err := store.OpenSession(t.Context(), &samplerpb.OpenSessionRequest{
		SessionId: uuid.NewString(), Spec: &runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CENTRAL_SAMPLER, Cwd: "/fixture", Model: "test-model"},
		Executor: &sandboxpb.SandboxDestination{Owner: &sandboxpb.ServiceAccount{Namespace: "test", Name: "executor"}, Sandbox: "test-executor", SandboxUid: uuid.NewString()}, ExpectedIncarnationUid: uuid.NewString(),
	}, "test-account-uid", protocol.APIStyleResponses)
	if err != nil {
		t.Fatal(err)
	}
	return session
}

func admitExportEvent(ctx context.Context, store *Store, id string, index int) error {
	_, err := store.Admit(ctx, id, &commandpb.Command{CommandId: fmt.Sprintf("command-%d", index), Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "test input"}}}, "test-account-uid", nil)
	return err
}

func TestHistoryExportReconcilesLostAcknowledgementAndConcurrentCommits(t *testing.T) {
	store := newTestStore(t)
	session := exportFixtureSession(t, store)
	for i := 0; i < 40; i++ {
		if err := admitExportEvent(t.Context(), store, session.ID, i); err != nil {
			t.Fatal(err)
		}
	}
	history := &fixtureHistory{entries: map[uint64]*eventlogpb.EventEntry{}, loseAcknowledgement: true, received: make(chan uint64, 1024)}
	history.onEntry = func() error { return admitExportEvent(t.Context(), store, session.ID, 40) }
	target, token := startFixtureHistory(t, history)
	ctx, cancel := context.WithTimeout(t.Context(), 15*time.Second)
	defer cancel()
	if err := ExportSession(ctx, store, session.ID, target, token); err == nil {
		t.Fatal("first export did not observe the lost acknowledgement")
	}
	if err := ExportSession(ctx, store, session.ID, target, token); err != nil {
		t.Fatal(err)
	}
	current, err := store.GetSession(ctx, session.ID, "")
	if err != nil {
		t.Fatal(err)
	}
	checkpoint, err := store.Checkpoint(ctx, session.ID, target)
	if err != nil || checkpoint == nil || *checkpoint != current.Cursor {
		t.Fatalf("checkpoint=%v cursor=%v err=%v", checkpoint, current.Cursor, err)
	}
	expected, err := store.Events(ctx, session.ID, 0, 1024)
	if err != nil {
		t.Fatal(err)
	}
	history.mu.Lock()
	for _, entry := range expected {
		if !proto.Equal(history.entries[entry.GetCursor()], entry) {
			t.Errorf("event %d changed", entry.GetCursor())
		}
	}
	if len(history.entries) != len(expected) {
		t.Errorf("entries=%d expected=%d", len(history.entries), len(expected))
	}
	if len(history.snapshots) == 0 || history.snapshots[len(history.snapshots)-1].GetLastCursor() != current.Cursor {
		t.Error("final announcement snapshot missing")
	}
	history.mu.Unlock()
	if _, err := store.DB().ExecContext(ctx, "DELETE FROM sampler_export_checkpoint"); err != nil {
		t.Fatal(err)
	}
	if err := ExportSession(ctx, store, session.ID, target, token); err != nil {
		t.Fatal(err)
	}
	checkpoint, err = store.Checkpoint(ctx, session.ID, target)
	if err != nil || checkpoint == nil || *checkpoint != current.Cursor {
		t.Fatalf("reconciled checkpoint=%v err=%v", checkpoint, err)
	}
}

func TestHistoryExporterWakesForAnotherWriterAndListenerReconnect(t *testing.T) {
	store, databaseURL := newTestStoreWithDSN(t)
	session := exportFixtureSession(t, store)
	history := &fixtureHistory{entries: map[uint64]*eventlogpb.EventEntry{}, received: make(chan uint64, 1024)}
	target, token := startFixtureHistory(t, history)
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	dsn, err := url.Parse(databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	query := dsn.Query()
	query.Set("application_name", "central-sampler-export-fixture")
	dsn.RawQuery = query.Encode()
	hub, err := wakeups.Listen(ctx, dsn.String(), OutboxChannel)
	if err != nil {
		t.Fatal(err)
	}
	defer hub.Close()
	stopped := make(chan error, 1)
	go func() { stopped <- RunExporter(ctx, store, target, token, hub) }()
	defer func() {
		cancel()
		if err := <-stopped; err != nil && !errors.Is(err, context.Canceled) {
			t.Errorf("exporter stopped: %v", err)
		}
	}()
	await := func(expected uint64) {
		t.Helper()
		for {
			select {
			case cursor := <-history.received:
				if cursor >= expected {
					return
				}
			case <-ctx.Done():
				t.Fatal("History did not receive committed event")
			}
		}
	}
	await(session.Cursor)
	for i := 0; i < 2; i++ {
		if i == 1 {
			if _, err := store.DB().ExecContext(ctx, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='central-sampler-export-fixture'"); err != nil {
				t.Fatal(err)
			}
		}
		// A separate Store represents another replica committing during an idle or disconnected LISTEN.
		writer := New(store.DB())
		if err := admitExportEvent(ctx, writer, session.ID, i); err != nil {
			t.Fatal(err)
		}
		current, err := store.GetSession(ctx, session.ID, "")
		if err != nil {
			t.Fatal(err)
		}
		await(current.Cursor)
	}
}

func TestHistoryExporterRotatesOneSessionAcrossFiniteQuanta(t *testing.T) {
	store, databaseURL := newTestStoreWithDSN(t)
	session := exportFixtureSession(t, store)
	for i := 0; i < 2*exportPageSize*exportPagesPerTurn+1; i++ {
		if err := admitExportEvent(t.Context(), store, session.ID, i); err != nil {
			t.Fatal(err)
		}
	}
	current, err := store.GetSession(t.Context(), session.ID, "")
	if err != nil {
		t.Fatal(err)
	}
	// Starting at the only ID must wrap around to it rather than exclude it forever.
	pending, err := store.PendingSessions(t.Context(), "history-fixture", nil, session.ID, 1)
	if err != nil || len(pending) != 1 || pending[0].ID != session.ID {
		t.Fatalf("rotating pending sessions=%v err=%v", pending, err)
	}
	history := &fixtureHistory{entries: map[uint64]*eventlogpb.EventEntry{}, received: make(chan uint64, 1024)}
	target, token := startFixtureHistory(t, history)
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	hub, err := wakeups.Listen(ctx, databaseURL, OutboxChannel)
	if err != nil {
		t.Fatal(err)
	}
	defer hub.Close()
	stopped := make(chan error, 1)
	go func() { stopped <- RunExporter(ctx, store, target, token, hub) }()
	defer func() {
		cancel()
		if err := <-stopped; err != nil && !errors.Is(err, context.Canceled) {
			t.Errorf("exporter stopped: %v", err)
		}
	}()
	for {
		select {
		case cursor := <-history.received:
			if cursor == current.Cursor {
				return
			}
		case <-ctx.Done():
			t.Fatal("finite quantum prevented later evidence from draining")
		}
	}
}
