package ingress

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
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const testSessionID = "ingress-session"

func newTestSource(t *testing.T, count int) (Source, *retained.Store, []*eventlogpb.EventEntry) {
	t.Helper()
	journalPath := filepath.Join(t.TempDir(), "journal.sqlite")
	j, err := journal.Open(context.Background(), journalPath, "event-source")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := j.Close(); err != nil {
			t.Errorf("close journal: %v", err)
		}
	})
	store, err := retained.NewStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if err := j.WithBatch(context.Background(), func(ctx context.Context) error {
		for i := 1; i <= count; i++ {
			if _, err := j.Append(ctx, &eventpb.Event{
				Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: fmt.Sprintf("line %d", i)}},
			}, journal.AppendOptions{}); err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	entries, err := j.Since(context.Background(), 0, max(1, count))
	if err != nil {
		t.Fatal(err)
	}
	source := Source{
		ID: testSessionID, Journal: j,
		Announce: func() *runnerpb.Attached {
			return &runnerpb.Attached{
				SessionId:  testSessionID,
				Spec:       &runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CLAUDE, Cwd: "/workspace", Model: "test-model"},
				LastCursor: j.LastCursor(),
			}
		},
	}
	return source, store, entries
}

func startGRPCServer(t *testing.T, register func(*grpc.Server)) string {
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

func newRPC(t *testing.T, target string) ingresspb.RunnerIngressClient {
	t.Helper()
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithNoProxy())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return ingresspb.NewRunnerIngressClient(conn)
}

func authenticatedContext(ctx context.Context) context.Context {
	return metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer test-ingress-token"))
}

func await[T any](t *testing.T, ctx context.Context, ch <-chan T, what string) T {
	t.Helper()
	select {
	case value := <-ch:
		return value
	case <-ctx.Done():
		t.Fatalf("timed out waiting for %s: %v", what, ctx.Err())
		var zero T
		return zero
	}
}

func checkAcknowledgementStored(store *retained.Store) func(string, retained.Acknowledgement) error {
	return func(id string, want retained.Acknowledgement) error {
		got, err := store.ReadAcknowledgement(id)
		if err != nil {
			return err
		}
		if got == nil || got.Cursor != want.Cursor || got.Sealed != want.Sealed {
			return fmt.Errorf("observed acknowledgement before durable write: got %+v, want %+v", got, want)
		}
		return nil
	}
}

type reconnectIngress struct {
	ingresspb.UnimplementedRunnerIngressServer
	calls      atomic.Int32
	announced  chan uint64
	entries    chan []*eventlogpb.EventEntry
	authorized chan string
	result     chan error
}

func (s *reconnectIngress) Push(stream grpc.BidiStreamingServer[ingresspb.PushFrame, ingresspb.Acknowledgement]) (resultErr error) {
	defer func() { s.result <- resultErr }()
	if values, ok := metadata.FromIncomingContext(stream.Context()); ok {
		s.authorized <- first(values.Get("authorization"))
	}
	frame, err := stream.Recv()
	if err != nil {
		return err
	}
	announce := frame.GetAnnounce()
	if announce == nil {
		return errors.New("first ingress frame was not Announce")
	}
	s.announced <- announce.GetLastCursor()
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: 1}); err != nil {
		return err
	}
	index := s.calls.Add(1)
	if index == 1 {
		frame, err := stream.Recv()
		if err != nil {
			return err
		}
		if entry := frame.GetEntry(); entry == nil || entry.GetCursor() != 2 {
			return errors.New("first attempt did not replay cursor 2")
		}
		return status.Error(codes.Unavailable, "history replica restarted")
	}
	var entries []*eventlogpb.EventEntry
	for cursor := uint64(2); cursor <= 4; cursor++ {
		frame, err = stream.Recv()
		if err != nil {
			return err
		}
		entry := frame.GetEntry()
		if entry == nil || entry.GetCursor() != cursor {
			return fmt.Errorf("reconnect cursor %d = %v", cursor, frame.GetFrame())
		}
		entries = append(entries, entry)
	}
	s.entries <- entries
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: 4}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return stream.Context().Err()
}

func first(values []string) string {
	if len(values) == 0 {
		return ""
	}
	return values[0]
}

func TestDeliverReconnectsWithFreshAnnouncement(t *testing.T) {
	source, store, want := newTestSource(t, 3)
	service := &reconnectIngress{
		announced:  make(chan uint64, 2),
		entries:    make(chan []*eventlogpb.EventEntry, 1),
		authorized: make(chan string, 2),
		result:     make(chan error, 2),
	}
	target := startGRPCServer(t, func(server *grpc.Server) {
		ingresspb.RegisterRunnerIngressServer(server, service)
	})
	ctx, cancel := context.WithTimeout(authenticatedContext(context.Background()), 8*time.Second)
	defer cancel()

	secondAnnounce := make(chan struct{})
	continueAnnounce := make(chan struct{})
	var announceCalls atomic.Int32
	source.Announce = func() *runnerpb.Attached {
		if announceCalls.Add(1) == 2 {
			close(secondAnnounce)
			<-continueAnnounce
		}
		return &runnerpb.Attached{SessionId: testSessionID, LastCursor: source.Journal.LastCursor()}
	}

	observed := make(chan retained.Acknowledgement, 4)
	client := NewClient(newRPC(t, target), store, func(id string, ack retained.Acknowledgement) error {
		if err := checkAcknowledgementStored(store)(id, ack); err != nil {
			return err
		}
		observed <- ack
		return nil
	})
	delivered := make(chan error, 1)
	go func() { delivered <- client.Deliver(ctx, source) }()
	if got := await(t, ctx, service.announced, "first announcement"); got != 3 {
		t.Fatalf("first announcement last cursor = %d, want 3", got)
	}
	if got := await(t, ctx, service.authorized, "first authorization"); got != "Bearer test-ingress-token" {
		t.Fatalf("first ingress authorization = %q", got)
	}
	if ack := await(t, ctx, observed, "first durable acknowledgement"); ack.Cursor != 1 || ack.Sealed {
		t.Fatalf("first acknowledgement = %+v", ack)
	}
	if err := await(t, ctx, service.result, "first failed stream"); status.Code(err) != codes.Unavailable {
		t.Fatalf("first stream error = %v, want Unavailable", err)
	}
	await(t, ctx, secondAnnounce, "reconnect announcement callback")
	newEntry, err := source.Journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: "after reconnect"}},
	}, journal.AppendOptions{})
	if err != nil {
		t.Fatal(err)
	}
	close(continueAnnounce)
	if got := await(t, ctx, service.announced, "fresh reconnect announcement"); got != 4 {
		t.Fatalf("reconnect announcement last cursor = %d, want 4", got)
	}
	if got := await(t, ctx, service.authorized, "reconnect authorization"); got != "Bearer test-ingress-token" {
		t.Fatalf("reconnect ingress authorization = %q", got)
	}
	entries := await(t, ctx, service.entries, "replayed entries")
	if len(entries) != 3 {
		t.Fatalf("reconnect entries = %d, want cursors 2 through 4", len(entries))
	}
	wantReplay := append(append([]*eventlogpb.EventEntry(nil), want[1:]...), newEntry)
	for index, entry := range entries {
		wantEntry := wantReplay[index]
		if !proto.Equal(entry, wantEntry) {
			t.Fatalf("reconnect entry %d = %v, want %v", index, entry, wantEntry)
		}
	}
	if ack := await(t, ctx, observed, "persisted reconnect prefix acknowledgement"); ack.Cursor != 1 || ack.Sealed {
		t.Fatalf("reconnect prefix acknowledgement = %+v", ack)
	}
	if ack := await(t, ctx, observed, "reconnect acknowledgement"); ack.Cursor != 4 || ack.Sealed {
		t.Fatalf("reconnect acknowledgement = %+v", ack)
	}
	cancel()
	joinCtx, joinCancel := context.WithTimeout(context.Background(), time.Second)
	defer joinCancel()
	if err := await(t, joinCtx, delivered, "delivery cancellation"); err != nil {
		t.Fatalf("delivery after cancellation = %v", err)
	}
	if err := await(t, joinCtx, service.result, "reconnected stream shutdown"); !errors.Is(err, context.Canceled) {
		t.Fatalf("reconnected server stream shutdown = %v", err)
	}
}

type beyondSentIngress struct {
	ingresspb.UnimplementedRunnerIngressServer
	result chan error
}

func (s *beyondSentIngress) Push(stream grpc.BidiStreamingServer[ingresspb.PushFrame, ingresspb.Acknowledgement]) (resultErr error) {
	defer func() { s.result <- resultErr }()
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
	for cursor := uint64(1); cursor <= inFlightLimit; cursor++ {
		frame, err = stream.Recv()
		if err != nil {
			return err
		}
		if entry := frame.GetEntry(); entry == nil || entry.GetCursor() != cursor {
			return fmt.Errorf("entry %d = %v", cursor, frame.GetFrame())
		}
	}
	return stream.Send(&ingresspb.Acknowledgement{Cursor: inFlightLimit + 1})
}

func TestIngressRejectsAcknowledgementBeyondSentCursor(t *testing.T) {
	source, store, _ := newTestSource(t, inFlightLimit+6)
	service := &beyondSentIngress{result: make(chan error, 1)}
	target := startGRPCServer(t, func(server *grpc.Server) {
		ingresspb.RegisterRunnerIngressServer(server, service)
	})
	ctx, cancel := context.WithTimeout(authenticatedContext(context.Background()), 8*time.Second)
	defer cancel()

	client := NewClient(newRPC(t, target), store, nil)
	progress, err := client.pushOnce(ctx, source)
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("ack beyond sent cursor = progress %t, err %v", progress, err)
	}
	if err := await(t, ctx, service.result, "invalid acknowledgement protocol"); err != nil {
		t.Fatal(err)
	}
	ack, err := store.ReadAcknowledgement(source.ID)
	if err != nil || ack == nil || ack.Cursor != 0 || ack.Sealed {
		t.Fatalf("invalid acknowledgement was persisted: ack=%+v err=%v", ack, err)
	}
}

type windowIngress struct {
	ingresspb.UnimplementedRunnerIngressServer
	windowSize int
	release    chan struct{}
	windowRead chan struct{}
	checked    chan struct{}
	premature  chan receivedFrame
	result     chan error
}

type receivedFrame struct {
	frame *ingresspb.PushFrame
	err   error
}

func (s *windowIngress) Push(stream grpc.BidiStreamingServer[ingresspb.PushFrame, ingresspb.Acknowledgement]) (resultErr error) {
	defer func() { s.result <- resultErr }()
	if frame, err := stream.Recv(); err != nil || frame.GetAnnounce() == nil {
		return errors.New("first ingress frame was not Announce")
	}
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: 0}); err != nil {
		return err
	}
	for cursor := uint64(1); cursor <= uint64(s.windowSize); cursor++ {
		frame, err := stream.Recv()
		if err != nil {
			return err
		}
		if entry := frame.GetEntry(); entry == nil || entry.GetCursor() != cursor {
			return fmt.Errorf("first window cursor %d = %v", cursor, frame.GetFrame())
		}
	}
	close(s.windowRead)
	next := make(chan *ingresspb.PushFrame, 1)
	go func() {
		frame, err := stream.Recv()
		if err != nil {
			s.premature <- receivedFrame{err: err}
			return
		}
		next <- frame
	}()
	timer := time.NewTimer(200 * time.Millisecond)
	defer timer.Stop()
	select {
	case frame := <-next:
		s.premature <- receivedFrame{frame: frame}
		return errors.New("runner sent a frame before the window was acknowledged")
	case <-timer.C:
		close(s.checked)
	case <-stream.Context().Done():
		return stream.Context().Err()
	}
	select {
	case <-s.release:
	case <-stream.Context().Done():
		return stream.Context().Err()
	}
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: uint64(s.windowSize)}); err != nil {
		return err
	}
	frame := <-next
	var err error
	for cursor := uint64(s.windowSize + 1); cursor <= uint64(s.windowSize+6); cursor++ {
		if entry := frame.GetEntry(); entry == nil || entry.GetCursor() != cursor {
			return fmt.Errorf("second window cursor %d = %v", cursor, frame.GetFrame())
		}
		if cursor < uint64(s.windowSize+6) {
			frame, err = stream.Recv()
			if err != nil {
				return err
			}
		}
	}
	frame, err = stream.Recv()
	if err != nil {
		return err
	}
	seal := frame.GetSealed()
	if seal == nil || seal.GetCursor() != uint64(s.windowSize+6) || seal.GetIncarnationUid() != "test-incarnation" {
		return fmt.Errorf("seal frame = %v", frame.GetFrame())
	}
	return stream.Send(&ingresspb.Acknowledgement{Cursor: seal.GetCursor(), Sealed: true})
}

func TestBackpressureSendsSealAfterTheFinalAcknowledgedWindow(t *testing.T) {
	const windowSize = 1024
	source, store, _ := newTestSource(t, windowSize+6)
	sealCursor, err := source.Journal.Seal(context.Background(), "test-incarnation")
	if err != nil || sealCursor != windowSize+6 {
		t.Fatalf("journal seal = cursor %d, err %v", sealCursor, err)
	}
	service := &windowIngress{
		windowSize: windowSize,
		release:    make(chan struct{}),
		windowRead: make(chan struct{}),
		checked:    make(chan struct{}),
		premature:  make(chan receivedFrame, 1),
		result:     make(chan error, 1),
	}
	target := startGRPCServer(t, func(server *grpc.Server) {
		ingresspb.RegisterRunnerIngressServer(server, service)
	})
	ctx, cancel := context.WithTimeout(authenticatedContext(context.Background()), 10*time.Second)
	defer cancel()
	client := NewClient(newRPC(t, target), store, checkAcknowledgementStored(store))
	delivered := make(chan error, 1)
	go func() {
		_, err := client.pushOnce(ctx, source)
		delivered <- err
	}()
	await(t, ctx, service.windowRead, "first in-flight window")
	await(t, ctx, service.checked, "in-flight cap check")
	select {
	case received := <-service.premature:
		t.Fatalf("runner did not remain behind the in-flight cap: frame=%v err=%v", received.frame, received.err)
	default:
	}
	if ack, err := store.ReadAcknowledgement(source.ID); err != nil || ack == nil || ack.Cursor != 0 || ack.Sealed {
		t.Fatalf("acknowledgement before window release = %+v, err %v", ack, err)
	}
	close(service.release)
	if err := await(t, ctx, delivered, "sealed push"); err != nil {
		t.Fatalf("push failed: %v", err)
	}
	if err := await(t, ctx, service.result, "window server completion"); err != nil {
		t.Fatal(err)
	}
	if ack, err := store.ReadAcknowledgement(source.ID); err != nil || ack == nil || ack.Cursor != sealCursor || !ack.Sealed {
		t.Fatalf("persisted sealed acknowledgement = %+v, err %v", ack, err)
	}
}

type liveJournalIngress struct {
	ingresspb.UnimplementedRunnerIngressServer
	announced chan struct{}
	entrySeen chan *eventlogpb.EventEntry
	sealSeen  chan *runnerpb.Sealed
	result    chan error
}

func (s *liveJournalIngress) Push(stream grpc.BidiStreamingServer[ingresspb.PushFrame, ingresspb.Acknowledgement]) (resultErr error) {
	defer func() { s.result <- resultErr }()
	if frame, err := stream.Recv(); err != nil || frame.GetAnnounce() == nil {
		return errors.New("first ingress frame was not Announce")
	}
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: 0}); err != nil {
		return err
	}
	close(s.announced)
	frame, err := stream.Recv()
	if err != nil {
		return err
	}
	entry := frame.GetEntry()
	if entry == nil || entry.GetCursor() != 1 {
		return errors.New("append did not wake the journal publisher")
	}
	s.entrySeen <- entry
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: 1}); err != nil {
		return err
	}
	frame, err = stream.Recv()
	if err != nil {
		return err
	}
	sealed := frame.GetSealed()
	if sealed == nil || sealed.GetCursor() != 1 {
		return errors.New("seal did not wake the journal publisher")
	}
	s.sealSeen <- sealed
	return stream.Send(&ingresspb.Acknowledgement{Cursor: 1, Sealed: true})
}

func TestJournalAppendAndSealWakeDeliveryWithoutRunnerNotification(t *testing.T) {
	source, store, _ := newTestSource(t, 0)
	service := &liveJournalIngress{announced: make(chan struct{}), entrySeen: make(chan *eventlogpb.EventEntry, 1), sealSeen: make(chan *runnerpb.Sealed, 1), result: make(chan error, 1)}
	target := startGRPCServer(t, func(server *grpc.Server) {
		ingresspb.RegisterRunnerIngressServer(server, service)
	})
	ctx, cancel := context.WithTimeout(authenticatedContext(context.Background()), 5*time.Second)
	defer cancel()
	observed := make(chan retained.Acknowledgement, 3)
	client := NewClient(newRPC(t, target), store, func(id string, ack retained.Acknowledgement) error {
		if err := checkAcknowledgementStored(store)(id, ack); err != nil {
			return err
		}
		observed <- ack
		return nil
	})
	delivered := make(chan error, 1)
	go func() {
		_, err := client.pushOnce(ctx, source)
		delivered <- err
	}()
	await(t, ctx, service.announced, "empty journal announcement")
	if ack := await(t, ctx, observed, "empty-prefix acknowledgement"); ack.Cursor != 0 || ack.Sealed {
		t.Fatalf("empty-prefix acknowledgement = %+v", ack)
	}
	appended, err := source.Journal.Append(ctx, &eventpb.Event{
		Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: "live"}},
	}, journal.AppendOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if got := await(t, ctx, service.entrySeen, "appended journal entry"); !proto.Equal(got, appended) {
		t.Fatalf("live entry = %v, want %v", got, appended)
	}
	if ack := await(t, ctx, observed, "appended entry acknowledgement"); ack.Cursor != 1 || ack.Sealed {
		t.Fatalf("appended entry acknowledgement = %+v", ack)
	}
	if cursor, err := source.Journal.Seal(ctx, "test-incarnation"); err != nil || cursor != 1 {
		t.Fatalf("journal seal = cursor %d, err %v", cursor, err)
	}
	if sealed := await(t, ctx, service.sealSeen, "journal seal frame"); sealed.GetCursor() != 1 {
		t.Fatalf("sealed frame = %v", sealed)
	}
	if err := await(t, ctx, delivered, "completed journal delivery"); err != nil {
		t.Fatalf("push failed: %v", err)
	}
	if err := await(t, ctx, service.result, "live journal server completion"); err != nil {
		t.Fatal(err)
	}
	if ack, err := store.ReadAcknowledgement(source.ID); err != nil || ack == nil || ack.Cursor != 1 || !ack.Sealed {
		t.Fatalf("persisted live sealed acknowledgement = %+v, err %v", ack, err)
	}
}

type emptySealIngress struct {
	ingresspb.UnimplementedRunnerIngressServer
	announced chan struct{}
	result    chan error
}

func (s *emptySealIngress) Push(stream grpc.BidiStreamingServer[ingresspb.PushFrame, ingresspb.Acknowledgement]) (resultErr error) {
	defer func() { s.result <- resultErr }()
	if frame, err := stream.Recv(); err != nil || frame.GetAnnounce() == nil {
		return errors.New("first ingress frame was not Announce")
	}
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: 0}); err != nil {
		return err
	}
	close(s.announced)
	frame, err := stream.Recv()
	if err != nil {
		return err
	}
	seal := frame.GetSealed()
	if seal == nil || seal.GetCursor() != 0 {
		return errors.New("empty journal seal did not wake the publisher")
	}
	return stream.Send(&ingresspb.Acknowledgement{Cursor: 0, Sealed: true})
}

func TestSealWakesDeliveryForAnEmptyJournal(t *testing.T) {
	source, store, _ := newTestSource(t, 0)
	service := &emptySealIngress{announced: make(chan struct{}), result: make(chan error, 1)}
	target := startGRPCServer(t, func(server *grpc.Server) {
		ingresspb.RegisterRunnerIngressServer(server, service)
	})
	ctx, cancel := context.WithTimeout(authenticatedContext(context.Background()), 5*time.Second)
	defer cancel()
	observed := make(chan retained.Acknowledgement, 2)
	client := NewClient(newRPC(t, target), store, func(id string, ack retained.Acknowledgement) error {
		if err := checkAcknowledgementStored(store)(id, ack); err != nil {
			return err
		}
		observed <- ack
		return nil
	})
	delivered := make(chan error, 1)
	go func() {
		_, err := client.pushOnce(ctx, source)
		delivered <- err
	}()
	await(t, ctx, service.announced, "empty journal announcement")
	await(t, ctx, observed, "initial empty acknowledgement")
	if cursor, err := source.Journal.Seal(ctx, "test-incarnation"); err != nil || cursor != 0 {
		t.Fatalf("empty journal seal = cursor %d, err %v", cursor, err)
	}
	if err := await(t, ctx, delivered, "empty sealed journal delivery"); err != nil {
		t.Fatalf("push failed: %v", err)
	}
	if err := await(t, ctx, service.result, "empty seal server completion"); err != nil {
		t.Fatal(err)
	}
	if ack, err := store.ReadAcknowledgement(source.ID); err != nil || ack == nil || ack.Cursor != 0 || !ack.Sealed {
		t.Fatalf("persisted empty seal acknowledgement = %+v, err %v", ack, err)
	}
}

func TestObserverFailureIsLocalAfterDurableAcknowledgement(t *testing.T) {
	source, store, _ := newTestSource(t, 0)
	var calls atomic.Int32
	service := &emptySealIngress{announced: make(chan struct{}), result: make(chan error, 1)}
	target := startGRPCServer(t, func(server *grpc.Server) {
		ingresspb.RegisterRunnerIngressServer(server, service)
	})
	ctx, cancel := context.WithTimeout(authenticatedContext(context.Background()), 5*time.Second)
	defer cancel()
	want := errors.New("COSI acknowledgement observation failed")
	client := NewClient(newRPC(t, target), store, func(id string, ack retained.Acknowledgement) error {
		calls.Add(1)
		if err := checkAcknowledgementStored(store)(id, ack); err != nil {
			return err
		}
		return want
	})
	err := client.Deliver(ctx, source)
	if !errors.Is(err, want) || calls.Load() != 1 {
		t.Fatalf("observer failure retried: calls=%d err=%v", calls.Load(), err)
	}
	if ack, err := store.ReadAcknowledgement(source.ID); err != nil || ack == nil || ack.Cursor != 0 || ack.Sealed {
		t.Fatalf("acknowledgement was not durable before observer failure: %+v, err %v", ack, err)
	}
}

func TestJournalReadFailureStopsDelivery(t *testing.T) {
	source, store, _ := newTestSource(t, 0)
	service := &emptySealIngress{announced: make(chan struct{}), result: make(chan error, 1)}
	target := startGRPCServer(t, func(server *grpc.Server) {
		ingresspb.RegisterRunnerIngressServer(server, service)
	})
	ctx, cancel := context.WithTimeout(authenticatedContext(context.Background()), 5*time.Second)
	defer cancel()
	var calls atomic.Int32
	client := NewClient(newRPC(t, target), store, func(id string, ack retained.Acknowledgement) error {
		if err := checkAcknowledgementStored(store)(id, ack); err != nil {
			return err
		}
		calls.Add(1)
		return source.Journal.Close()
	})
	delivered := make(chan error, 1)
	go func() { delivered <- client.Deliver(ctx, source) }()
	await(t, ctx, service.announced, "empty journal announcement")
	err := await(t, ctx, delivered, "local journal failure")
	var local *localError
	if err == nil || !errors.As(err, &local) || calls.Load() != 1 || ctx.Err() != nil {
		t.Fatalf("journal failure retried or timed out: callbacks=%d err=%v ctxerr=%v", calls.Load(), err, ctx.Err())
	}
	if err := await(t, ctx, service.result, "journal failure stream cancellation"); err == nil {
		t.Fatal("ingress stream remained open after a local journal failure")
	}
}
