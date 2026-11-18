package rpc

import (
	"context"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/harness_host"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionconfig"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/testsupport"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func newHostFixture(t *testing.T) (*HarnessHost, string) {
	t.Helper()
	dsn := rpcIntegrationDatabase.Clone(t)
	store := sessionstore.New(postgres.Open(t, dsn), 30*time.Second)
	hub, err := wakeups.Listen(t.Context(), dsn, sessionstore.OutboxChannel, sessionstore.WorkChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = hub.Close() })
	settings := fixtureSettings()
	settings.ServiceCallers = append(settings.ServiceCallers, peertls.ActionsURI)
	return &HarnessHost{Sampler: &Sampler{Store: store, Runtime: &residentruntime.Runtime{}, Settings: settings, Wakeups: hub}}, dsn
}

func hostCreateRequest() *harnesshostpb.CreateSessionRequest {
	r := &sessionconfig.Configuration{SessionId: uuid.NewString(), Spec: &sessionpb.SessionSpec{Cwd: "/fixture", Model: "test-model"}}
	testsupport.FillSessionConfigurationDefaults(r)
	return &harnesshostpb.CreateSessionRequest{SessionId: r.SessionId, Spec: r.Spec, ExecutionIdentity: r.ExecutionIdentity}
}

func TestHarnessHostUsesSamplerAdmissionAndFencing(t *testing.T) {
	h, _ := newHostFixture(t)
	transport := &hostTransport{}
	ctx := grpc.NewContextWithServerTransportStream(serviceContext(t, peertls.SessionsURI), transport)
	request := hostCreateRequest()
	if _, err := h.CreateSession(ctx, request); err != nil {
		t.Fatal(err)
	}
	// An exact retry confirms the immutable admission row.
	again, err := h.CreateSession(ctx, request)
	if err != nil || again.GetAttached() == nil {
		t.Fatalf("retry=%v err=%v", again, err)
	}
	changed := proto.Clone(request).(*harnesshostpb.CreateSessionRequest)
	changed.Spec.Cwd = "/changed"
	if _, err := h.CreateSession(ctx, changed); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("changed creation=%v", err)
	}
	input := &harnesshostpb.SubmitInputRequest{SessionId: request.SessionId, CommandId: "input", Text: "hello"}
	admitted, err := h.SubmitInput(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	retry, err := h.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{SessionId: request.SessionId, CommandId: "input", Text: "hello"})
	if err != nil || !proto.Equal(admitted.GetAdmission(), retry.GetAdmission()) {
		t.Fatalf("shared admission=%v err=%v", retry, err)
	}
	input.Text = "different"
	if _, err := h.SubmitInput(ctx, input); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("command collision=%v", err)
	}
	if _, err := h.SubmitInput(serviceContext(t, peertls.ActionsURI), input); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("cross-owner admission=%v", err)
	}
	if _, err := h.ChangeModel(ctx, &harnesshostpb.ChangeModelRequest{SessionId: request.SessionId, CommandId: "model", Model: "test-model"}); err != nil {
		t.Fatal(err)
	}
	if _, err := h.ChangeReasoningEffort(ctx, &harnesshostpb.ChangeReasoningEffortRequest{SessionId: request.SessionId, CommandId: "effort", Effort: "high"}); err != nil {
		t.Fatal(err)
	}
	if _, err := h.InterruptTurn(ctx, &harnesshostpb.InterruptTurnRequest{SessionId: request.SessionId, CommandId: "stale", TurnId: "stale"}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("stale interrupt=%v", err)
	}
	session, err := h.Sampler.Store.GetSession(t.Context(), request.SessionId, peertls.SessionsURI)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := h.Sampler.Store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := h.Sampler.Store.NextCommand(t.Context(), owner, session.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := h.InterruptTurn(ctx, &harnesshostpb.InterruptTurnRequest{SessionId: session.ID, CommandId: "interrupt", TurnId: "input"}); err != nil {
		t.Fatal(err)
	}
	listed, err := h.ListSessions(ctx, &harnesshostpb.ListSessionsRequest{})
	if err != nil || len(listed.GetSessions()) != 1 || listed.Sessions[0].GetActiveTurnId() != "input" || listed.Sessions[0].GetPendingCommands() != 3 {
		t.Fatalf("list=%v err=%v", listed, err)
	}
	if err := harnesshost.RequireSessionVersion(transport.header); err != nil {
		t.Fatal(err)
	}
	hidden, err := h.ListSessions(grpc.NewContextWithServerTransportStream(serviceContext(t, peertls.ActionsURI), &hostTransport{}), &harnesshostpb.ListSessionsRequest{})
	if err != nil || len(hidden.GetSessions()) != 0 {
		t.Fatalf("cross-owner list=%v err=%v", hidden, err)
	}
}

type hostWatchStream struct {
	grpc.ServerStream
	ctx      context.Context
	messages chan *harnesshostpb.WatchSessionsResponse
	header   metadata.MD
}

func (s *hostWatchStream) SendHeader(header metadata.MD) error { s.header = header.Copy(); return nil }

func (s *hostWatchStream) Context() context.Context { return s.ctx }
func (s *hostWatchStream) Send(r *harnesshostpb.WatchSessionsResponse) error {
	select {
	case s.messages <- r:
		return nil
	case <-s.ctx.Done():
		return s.ctx.Err()
	}
}

func hostWatchBatch(t *testing.T, stream *hostWatchStream) []*harnesshostpb.Session {
	t.Helper()
	timer := time.NewTimer(5 * time.Second)
	defer timer.Stop()
	var sessions []*harnesshostpb.Session
	for {
		select {
		case frame := <-stream.messages:
			if frame.GetCaughtUp() != nil {
				if err := harnesshost.RequireSessionVersion(stream.header); err != nil {
					t.Fatal(err)
				}
				return sessions
			}
			if frame.GetUpsert() == nil {
				t.Fatalf("invalid frame=%v", frame)
			}
			sessions = append(sessions, frame.GetUpsert())
		case <-timer.C:
			t.Fatal("watch did not catch up")
			return nil
		}
	}
}

func TestHarnessHostWatchSnapshotChangesAndReconnect(t *testing.T) {
	h, _ := newHostFixture(t)
	ctx := serviceContext(t, peertls.SessionsURI)
	original := hostCreateRequest()
	if _, err := h.CreateSession(ctx, original); err != nil {
		t.Fatal(err)
	}
	if _, err := h.CreateSession(serviceContext(t, peertls.ActionsURI), hostCreateRequest()); err != nil {
		t.Fatal(err)
	}
	watchCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream := &hostWatchStream{ctx: watchCtx, messages: make(chan *harnesshostpb.WatchSessionsResponse, 32)}
	done := make(chan error, 1)
	go func() { done <- h.WatchSessions(&harnesshostpb.WatchSessionsRequest{}, stream) }()
	if batch := hostWatchBatch(t, stream); len(batch) != 1 || batch[0].SessionId != original.SessionId {
		t.Fatalf("snapshot=%v", batch)
	}
	created := hostCreateRequest()
	if _, err := h.CreateSession(ctx, created); err != nil {
		t.Fatal(err)
	}
	for {
		batch := hostWatchBatch(t, stream)
		if len(batch) == 0 {
			continue
		} // coalesced wakeups may include an unchanged batch
		if len(batch) != 1 || batch[0].SessionId != created.SessionId {
			t.Fatalf("new session batch=%v", batch)
		}
		break
	}
	if _, err := h.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{SessionId: original.SessionId, CommandId: "queued", Text: "hello"}); err != nil {
		t.Fatal(err)
	}
	for {
		batch := hostWatchBatch(t, stream)
		if len(batch) == 0 {
			continue
		}
		if len(batch) != 1 || batch[0].SessionId != original.SessionId || batch[0].PendingCommands != 1 {
			t.Fatalf("changed batch=%v", batch)
		}
		break
	}
	cancel()
	if err := <-done; status.Code(err) != codes.Canceled {
		t.Fatalf("watch end=%v", err)
	}
	// A new stream starts with every currently visible Session, including unchanged ones.
	reconnectCtx, reconnectCancel := context.WithCancel(ctx)
	reconnect := &hostWatchStream{ctx: reconnectCtx, messages: make(chan *harnesshostpb.WatchSessionsResponse, 32)}
	go func() { done <- h.WatchSessions(&harnesshostpb.WatchSessionsRequest{}, reconnect) }()
	if batch := hostWatchBatch(t, reconnect); len(batch) != 2 {
		t.Fatalf("reconnect snapshot=%v", batch)
	}
	reconnectCancel()
	<-done
}

func TestHarnessHostRejectsUnadmittedService(t *testing.T) {
	h, _ := newHostFixture(t)
	ctx := serviceContext(t, peertls.HistoryIngressURI)
	if _, err := h.CreateSession(ctx, hostCreateRequest()); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("creation=%v", err)
	}
	if _, err := h.ListSessions(ctx, &harnesshostpb.ListSessionsRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("list=%v", err)
	}
	stream := &hostWatchStream{ctx: ctx, messages: make(chan *harnesshostpb.WatchSessionsResponse, 1)}
	if err := h.WatchSessions(&harnesshostpb.WatchSessionsRequest{}, stream); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("watch=%v", err)
	}
	if len(stream.messages) != 0 {
		t.Fatal("unadmitted watch exposed status")
	}
}

func TestHarnessHostWatchEndsOnWakeupConnectionLoss(t *testing.T) {
	h, dsn := newHostFixture(t)
	ctx, cancel := context.WithCancel(serviceContext(t, peertls.SessionsURI))
	defer cancel()
	stream := &hostWatchStream{ctx: ctx, messages: make(chan *harnesshostpb.WatchSessionsResponse, 32)}
	done := make(chan error, 1)
	go func() { done <- h.WatchSessions(&harnesshostpb.WatchSessionsRequest{}, stream) }()
	if batch := hostWatchBatch(t, stream); len(batch) != 0 {
		t.Fatalf("initial snapshot=%v", batch)
	}
	// Losing LISTEN may lose committed changes. End the stream even if the
	// listener reconnects before its buffered wakeup is consumed.
	db := postgres.Open(t, dsn)
	var terminated bool
	if err := db.QueryRowContext(t.Context(), `SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE datname=current_database() AND pid<>pg_backend_pid() AND query LIKE 'LISTEN %'`).Scan(&terminated); err != nil {
		t.Fatal(err)
	}
	if !terminated {
		t.Fatal("listener was not terminated")
	}
	select {
	case err := <-done:
		if status.Code(err) != codes.Unavailable {
			t.Fatalf("disconnected watch=%v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("disconnected watch did not end")
	}
}

type hostTransport struct{ header metadata.MD }

func (s *hostTransport) Method() string { return "/agentplane.harness_host.HarnessHost/ListSessions" }

func (s *hostTransport) SetHeader(header metadata.MD) error {
	s.header = metadata.Join(s.header, header)
	return nil
}
func (s *hostTransport) SendHeader(header metadata.MD) error { return s.SetHeader(header) }
func (s *hostTransport) SetTrailer(metadata.MD) error        { return nil }
