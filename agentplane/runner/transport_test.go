package runner

import (
	"context"
	"errors"
	"fmt"
	"net"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/runner_ingresspb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	tunnelpb "github.com/agentydragon/ducktape/agentplane/proto/grpc_tunnel"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	controlpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func loopbackGRPCServer(t *testing.T, register func(*grpc.Server)) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	register(server)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	return listener.Addr().String()
}

type controlAudit struct {
	Authorization string
	Hello         *controlpb.Hello
	Facts         *controlpb.RunnerFacts
	Reply         *tunnelpb.Reply
	Err           error
}

type controlLoopback struct {
	controlpb.UnimplementedRunnerControlServer
	result chan controlAudit
}

func (s *controlLoopback) Connect(stream grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame]) (resultErr error) {
	audit := controlAudit{}
	defer func() {
		audit.Err = resultErr
		s.result <- audit
	}()
	if values, ok := metadata.FromIncomingContext(stream.Context()); ok {
		audit.Authorization = firstString(values.Get("authorization"))
	}
	frame, err := stream.Recv()
	if err != nil {
		return err
	}
	audit.Hello = frame.GetHello()
	if audit.Hello == nil {
		return errors.New("first control frame was not Hello")
	}
	if err := stream.Send(&controlpb.ServiceFrame{Frame: &controlpb.ServiceFrame_Welcome{Welcome: &controlpb.Welcome{ConnectionEpoch: 1, IncarnationUid: "loopback-incarnation"}}}); err != nil {
		return err
	}
	frame, err = stream.Recv()
	if err != nil {
		return err
	}
	if heartbeat := frame.GetHeartbeat(); heartbeat != nil {
		audit.Facts = heartbeat.GetFacts()
	}
	if audit.Facts == nil {
		return errors.New("runner did not publish facts after Welcome")
	}
	request, err := proto.Marshal(&runnerpb.ListSessionsRequest{})
	if err != nil {
		return err
	}
	if err := stream.Send(&controlpb.ServiceFrame{Frame: &controlpb.ServiceFrame_Call{Call: &tunnelpb.Call{
		CallId: 17, Method: "/agentplane.runner.Runner/ListSessions", Request: request,
	}}}); err != nil {
		return err
	}
	frame, err = stream.Recv()
	if err != nil {
		return err
	}
	reply := frame.GetReply()
	if reply == nil {
		return errors.New("runner did not answer tunneled ListSessions call")
	}
	audit.Reply = reply
	return nil
}

func firstString(values []string) string {
	if len(values) == 0 {
		return ""
	}
	return values[0]
}

func TestControlConnectAuthenticatesWelcomesPublishesFactsAndRelaysRPC(t *testing.T) {
	runner := newBareRunner(t)
	service := &controlLoopback{result: make(chan controlAudit, 1)}
	endpoint := loopbackGRPCServer(t, func(server *grpc.Server) {
		controlpb.RegisterRunnerControlServer(server, service)
	})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	connected := make(chan error, 1)
	go func() {
		_, err := runner.connectControl(ctx, Endpoint{Target: endpoint, Bearer: "loopback-token"})
		connected <- err
	}()
	var audit controlAudit
	select {
	case audit = <-service.result:
	case <-ctx.Done():
		t.Fatalf("control exchange timed out: %v", ctx.Err())
	}
	if audit.Err != nil {
		t.Fatalf("control stream failed: %v", audit.Err)
	}
	if audit.Authorization != "Bearer loopback-token" || audit.Hello == nil || audit.Hello.RunnerBootId != runner.bootID {
		t.Fatalf("control authentication/Hello = authorization %q, hello %v", audit.Authorization, audit.Hello)
	}
	if audit.Facts == nil || audit.Facts.IdleSince == nil || audit.Facts.QuiesceStage != controlpb.RunnerFacts_QUIESCE_STAGE_UNSPECIFIED {
		t.Fatalf("first heartbeat facts = %v", audit.Facts)
	}
	if uid, known := runner.store.IncarnationUID(); !known || uid != "loopback-incarnation" {
		t.Fatalf("Welcome incarnation = %q, %t", uid, known)
	}
	if audit.Reply == nil || audit.Reply.CallId != 17 || audit.Reply.Code != int32(codes.OK) {
		t.Fatalf("tunneled RPC reply = %v", audit.Reply)
	}
	response := &runnerpb.ListSessionsResponse{}
	if err := proto.Unmarshal(audit.Reply.Response, response); err != nil || len(response.Sessions) != 0 {
		t.Fatalf("tunneled ListSessions response = %v, %v", response, err)
	}
	select {
	case err := <-connected:
		if err == nil {
			t.Fatal("control stream ended successfully without cancellation")
		}
	case <-ctx.Done():
		t.Fatalf("control client did not exit after peer closed: %v", ctx.Err())
	}
}

type ingressCall struct {
	authorization string
	announce      *runnerpb.Attached
	entries       []*eventlogpb.EventEntry
	err           error
}

type replayIngress struct {
	ingresspb.UnimplementedRunnerIngressServer
	calls atomic.Int32
	seen  chan ingressCall
}

func (s *replayIngress) Push(stream grpc.BidiStreamingServer[ingresspb.PushFrame, ingresspb.Acknowledgement]) (resultErr error) {
	call := ingressCall{}
	defer func() {
		call.err = resultErr
		s.seen <- call
	}()
	if values, ok := metadata.FromIncomingContext(stream.Context()); ok {
		call.authorization = firstString(values.Get("authorization"))
	}
	frame, err := stream.Recv()
	if err != nil {
		return err
	}
	call.announce = frame.GetAnnounce()
	if call.announce == nil {
		return errors.New("first ingress frame was not Announce")
	}
	index := s.calls.Add(1)
	ackCursor, entryCount, finalCursor := uint64(1), 2, uint64(3)
	if index == 2 {
		ackCursor, entryCount, finalCursor = 3, 1, 4
	}
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: ackCursor}); err != nil {
		return err
	}
	for i := 0; i < entryCount; i++ {
		frame, err = stream.Recv()
		if err != nil {
			return err
		}
		entry := frame.GetEntry()
		if entry == nil {
			return fmt.Errorf("expected journal entry, got %v", frame.GetFrame())
		}
		call.entries = append(call.entries, entry)
	}
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: finalCursor}); err != nil {
		return err
	}
	return nil
}

func TestIngressReplayPersistsAcknowledgementAndResumesSameSourceAndCursors(t *testing.T) {
	runner, session := newPushSession(t, 3)
	want, err := session.journal.Since(context.Background(), 0, 8)
	if err != nil {
		t.Fatal(err)
	}
	service := &replayIngress{seen: make(chan ingressCall, 2)}
	endpoint := loopbackGRPCServer(t, func(server *grpc.Server) {
		ingresspb.RegisterRunnerIngressServer(server, service)
	})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	firstProgress, firstErr := runner.pushOnce(ctx, Endpoint{Target: endpoint, Bearer: "ingress-token"}, ingresspb.NewRunnerIngressClient(mustConnection(t, endpoint)), session)
	if !firstProgress || firstErr == nil {
		t.Fatalf("first push = progressed %t, err %v; expected acked progress then peer close", firstProgress, firstErr)
	}
	first := receiveIngressCall(t, ctx, service.seen)
	if first.err != nil || first.authorization != "Bearer ingress-token" || first.announce.GetSessionId() != session.id || first.announce.GetLastCursor() != 3 || len(first.entries) != 2 {
		t.Fatalf("first ingress call = %+v", first)
	}
	assertEntries(t, first.entries, want[1:3])
	ack, err := runner.store.ReadAcknowledgement(session.id)
	if err != nil || ack == nil || ack.Cursor != 3 || ack.Sealed {
		t.Fatalf("persisted first acknowledgement = %v, %v", ack, err)
	}

	newEntry, err := session.journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: "after reconnect"}},
	}, journal.AppendOptions{})
	if err != nil {
		t.Fatal(err)
	}
	_, secondErr := runner.pushOnce(ctx, Endpoint{Target: endpoint, Bearer: "ingress-token"}, ingresspb.NewRunnerIngressClient(mustConnection(t, endpoint)), session)
	if secondErr == nil {
		t.Fatal("second push unexpectedly stayed open after the server closed")
	}
	second := receiveIngressCall(t, ctx, service.seen)
	if second.err != nil || second.authorization != "Bearer ingress-token" || second.announce.GetSessionId() != session.id || second.announce.GetLastCursor() != 4 || len(second.entries) != 1 {
		t.Fatalf("second ingress call = %+v", second)
	}
	assertEntries(t, second.entries, []*eventlogpb.EventEntry{newEntry})
	if first.entries[0].GetOrigin().GetSourceId() != second.entries[0].GetOrigin().GetSourceId() || second.entries[0].GetOrigin().GetSequence() != 4 {
		t.Fatalf("reconnect changed EventOrigin or cursor: first=%v second=%v", first.entries[0].GetOrigin(), second.entries[0].GetOrigin())
	}
	ack, err = runner.store.ReadAcknowledgement(session.id)
	if err != nil || ack == nil || ack.Cursor != 4 || ack.Sealed {
		t.Fatalf("persisted reconnect acknowledgement = %v, %v", ack, err)
	}
}

func TestIngressRejectsAcknowledgementBeyondSentCursor(t *testing.T) {
	runner, session := newPushSession(t, ingressInFlight+6)
	service := &beyondSentIngress{err: make(chan error, 1)}
	endpoint := loopbackGRPCServer(t, func(server *grpc.Server) {
		ingresspb.RegisterRunnerIngressServer(server, service)
	})
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	conn := mustConnection(t, endpoint)
	defer conn.Close()
	progress, err := runner.pushOnce(ctx, Endpoint{Target: endpoint, Bearer: "ingress-token"}, ingresspb.NewRunnerIngressClient(conn), session)
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("ack beyond sent cursor = progress %t, err %v", progress, err)
	}
	select {
	case serverErr := <-service.err:
		if serverErr != nil {
			t.Fatal(serverErr)
		}
	case <-ctx.Done():
		t.Fatalf("ingress did not finish test protocol: %v", ctx.Err())
	}
	ack, readErr := runner.store.ReadAcknowledgement(session.id)
	if readErr != nil || ack == nil || ack.Cursor != 0 {
		t.Fatalf("invalid acknowledgement was persisted: ack=%v err=%v", ack, readErr)
	}
}

type beyondSentIngress struct {
	ingresspb.UnimplementedRunnerIngressServer
	err chan error
}

func (s *beyondSentIngress) Push(stream grpc.BidiStreamingServer[ingresspb.PushFrame, ingresspb.Acknowledgement]) (resultErr error) {
	defer func() { s.err <- resultErr }()
	frame, err := stream.Recv()
	if err != nil {
		return err
	}
	if frame.GetAnnounce() == nil {
		return errors.New("first ingress frame was not Announce")
	}
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: 0}); err != nil {
		return err
	}
	for i := 0; i < ingressInFlight; i++ {
		frame, err = stream.Recv()
		if err != nil {
			return err
		}
		if frame.GetEntry() == nil || frame.GetEntry().GetCursor() != uint64(i+1) {
			return fmt.Errorf("entry %d = %v", i+1, frame.GetFrame())
		}
	}
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: ingressInFlight + 1}); err != nil {
		return err
	}
	return nil
}

func TestIngressBoundsInflightAndSendsSealAfterFinalEntry(t *testing.T) {
	runner, session := newPushSession(t, ingressInFlight+6)
	sealCursor, err := session.journal.Seal(context.Background(), "push-incarnation")
	if err != nil || sealCursor != ingressInFlight+6 {
		t.Fatalf("journal seal = cursor %d, err %v", sealCursor, err)
	}
	service := &sealedIngress{
		firstBatch:      make(chan struct{}),
		releaseFirstAck: make(chan struct{}, 1),
		preAckChecked:   make(chan struct{}),
		premature:       make(chan ingresspb.PushFrame, 1),
		err:             make(chan error, 1),
	}
	endpoint := loopbackGRPCServer(t, func(server *grpc.Server) {
		ingresspb.RegisterRunnerIngressServer(server, service)
	})
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	conn := mustConnection(t, endpoint)
	defer conn.Close()
	result := make(chan pushResult, 1)
	go func() {
		progress, err := runner.pushOnce(ctx, Endpoint{Target: endpoint, Bearer: "ingress-token"}, ingresspb.NewRunnerIngressClient(conn), session)
		result <- pushResult{progress: progress, err: err}
	}()
	select {
	case <-service.firstBatch:
	case <-ctx.Done():
		t.Fatalf("ingress did not receive the first window: %v", ctx.Err())
	}
	select {
	case frame := <-service.premature:
		t.Fatalf("runner exceeded the in-flight cap before an acknowledgement: %v", frame.GetFrame())
	case <-service.preAckChecked:
	case <-ctx.Done():
		t.Fatalf("server did not complete its pre-ack window check: %v", ctx.Err())
	}
	service.releaseFirstAck <- struct{}{}
	select {
	case got := <-result:
		if got.err != nil || !got.progress {
			t.Fatalf("sealed push = %+v", got)
		}
	case <-ctx.Done():
		t.Fatalf("sealed ingress push timed out: %v", ctx.Err())
	}
	select {
	case err := <-service.err:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatalf("ingress test protocol did not finish: %v", ctx.Err())
	}
	ack, err := runner.store.ReadAcknowledgement(session.id)
	if err != nil || ack == nil || !ack.Sealed || ack.Cursor != sealCursor {
		t.Fatalf("persisted seal acknowledgement = %v, %v", ack, err)
	}
}

type sealedIngress struct {
	ingresspb.UnimplementedRunnerIngressServer
	firstBatch      chan struct{}
	releaseFirstAck chan struct{}
	preAckChecked   chan struct{}
	premature       chan ingresspb.PushFrame
	err             chan error
}

func (s *sealedIngress) Push(stream grpc.BidiStreamingServer[ingresspb.PushFrame, ingresspb.Acknowledgement]) (resultErr error) {
	defer func() { s.err <- resultErr }()
	frame, err := stream.Recv()
	if err != nil {
		return err
	}
	if frame.GetAnnounce() == nil {
		return errors.New("first ingress frame was not Announce")
	}
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: 0}); err != nil {
		return err
	}
	for cursor := uint64(1); cursor <= ingressInFlight; cursor++ {
		frame, err = stream.Recv()
		if err != nil {
			return err
		}
		if entry := frame.GetEntry(); entry == nil || entry.GetCursor() != cursor {
			return fmt.Errorf("first window cursor %d = %v", cursor, frame.GetFrame())
		}
	}
	close(s.firstBatch)
	next := make(chan *ingresspb.PushFrame, 1)
	go func() {
		item, err := stream.Recv()
		if err != nil {
			s.premature <- ingresspb.PushFrame{}
			return
		}
		next <- item
	}()
	select {
	case frame := <-next:
		s.premature <- *frame
		return errors.New("runner sent a frame before the window was acknowledged")
	case <-time.After(200 * time.Millisecond):
		close(s.preAckChecked)
	}
	select {
	case <-s.releaseFirstAck:
	case <-stream.Context().Done():
		return stream.Context().Err()
	}
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: ingressInFlight}); err != nil {
		return err
	}
	frame = <-next
	if entry := frame.GetEntry(); entry == nil || entry.GetCursor() != ingressInFlight+1 {
		return fmt.Errorf("entry after first window = %v", frame.GetFrame())
	}
	for cursor := uint64(ingressInFlight + 2); cursor <= ingressInFlight+6; cursor++ {
		frame, err = stream.Recv()
		if err != nil {
			return err
		}
		if entry := frame.GetEntry(); entry == nil || entry.GetCursor() != cursor {
			return fmt.Errorf("final-window cursor %d = %v", cursor, frame.GetFrame())
		}
	}
	frame, err = stream.Recv()
	if err != nil {
		return err
	}
	sealed := frame.GetSealed()
	if sealed == nil || sealed.GetCursor() != ingressInFlight+6 || sealed.GetIncarnationUid() != "push-incarnation" {
		return fmt.Errorf("seal arrived before final entries or has wrong identity: %v", frame.GetFrame())
	}
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: sealed.GetCursor(), Sealed: true}); err != nil {
		return err
	}
	return nil
}

type pushResult struct {
	progress bool
	err      error
}

func newBareRunner(t *testing.T) *Runner {
	t.Helper()
	runner, err := New(Config{StateDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	if err := runner.store.AdoptIncarnation("test-incarnation"); err != nil {
		_ = runner.owner.Close()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := runner.Close(); err != nil {
			t.Errorf("close test runner: %v", err)
		}
	})
	return runner
}

func newPushSession(t *testing.T, count int) (*Runner, *Session) {
	t.Helper()
	runner := newBareRunner(t)
	cwd := filepath.Join(t.TempDir(), "project")
	record, err := NewSessionRecord(&runnerpb.SessionSpec{
		Harness: runnerpb.Harness_HARNESS_CLAUDE, Cwd: cwd, Model: "test-model",
	}, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := runner.store.Write("push-session", record); err != nil {
		t.Fatal(err)
	}
	session, err := runner.load(context.Background(), "push-session")
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if err := session.journal.WithBatch(ctx, func(batchCtx context.Context) error {
		for index := 0; index < count; index++ {
			_, err := session.journal.Append(batchCtx, &eventpb.Event{
				Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: fmt.Sprintf("line %d", index)}},
			}, journal.AppendOptions{})
			if err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	return runner, session
}

func mustConnection(t *testing.T, endpoint string) *grpc.ClientConn {
	t.Helper()
	conn, err := connection(Endpoint{Target: endpoint})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

func receiveIngressCall(t *testing.T, ctx context.Context, calls <-chan ingressCall) ingressCall {
	t.Helper()
	select {
	case call := <-calls:
		return call
	case <-ctx.Done():
		t.Fatalf("ingress exchange timed out: %v", ctx.Err())
		return ingressCall{}
	}
}

func assertEntries(t *testing.T, got, want []*eventlogpb.EventEntry) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("received %d EventEntries, want %d", len(got), len(want))
	}
	for i := range want {
		if !proto.Equal(got[i], want[i]) {
			t.Fatalf("EventEntry %d = %v, want exact journal entry %v", i, got[i], want[i])
		}
	}
}

func TestStreamRetryPreservesLocalFailures(t *testing.T) {
	for _, failure := range []error{
		&journal.StorageError{Err: errors.New("journal failed")},
		&localStreamError{err: errors.New("acknowledgement failed")},
	} {
		calls := 0
		err := retryStream(context.Background(), "test", func(context.Context) (bool, error) {
			calls++
			return true, failure
		})
		if err != failure || calls != 1 {
			t.Fatalf("local error retried: %d calls, %v", calls, err)
		}
	}
}

func TestStreamRetryStopsOnCancellationDuringPermissionBackoff(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	calls := 0
	if err := retryStream(ctx, "test", func(context.Context) (bool, error) {
		calls++
		cancel()
		return false, status.Error(codes.PermissionDenied, "revoked")
	}); err != nil || calls != 1 {
		t.Fatalf("cancelled reconnect = %v after %d attempts", err, calls)
	}
}

func TestStreamRetryReconnectsAfterRemoteError(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	calls := 0
	if err := retryStream(ctx, "test", func(context.Context) (bool, error) {
		calls++
		if calls == 1 {
			return false, status.Error(codes.Unavailable, "disconnected")
		}
		return true, nil
	}); err != nil || calls != 2 {
		t.Fatalf("reconnect = %v after %d attempts", err, calls)
	}
}
