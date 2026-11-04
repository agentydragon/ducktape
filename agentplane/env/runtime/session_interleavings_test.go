package runtime

import (
	"context"
	"fmt"
	"net"
	"strings"
	"sync"
	"testing"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type incarnationWaitContextKey struct{}

type incarnationWaitState struct {
	state.State
	waiting chan struct{}
	once    sync.Once
}

func (s *incarnationWaitState) WatchFor(ctx context.Context, pointer resource.Pointer, conditions ...state.WatchForConditionFunc) (resource.Resource, error) {
	if ctx.Value(incarnationWaitContextKey{}) != nil && pointer.Type() == lifecycle.AggregateStatusType {
		// Only the marked adoption call can rendezvous here, after deciding teardown is incomplete.
		s.once.Do(func() { close(s.waiting) })
	}
	return s.State.WatchFor(ctx, pointer, conditions...)
}

// Each trace starts with a real setup process and a journal-blocked admission.
// Enumerate all orders of their release and a competing lifecycle operation;
// the oracle depends on outstanding work, not the controller's current phase.
func TestLifecycleBarrierInterleavings(t *testing.T) {
	for _, operation := range []string{"close", "replace", "acknowledge"} {
		events := []string{"admission", "setup", operation}
		for first := range events {
			for second := range events {
				if first == second {
					continue
				}
				trace := []string{events[first], events[second], events[3-first-second]}
				t.Run(fmt.Sprintf("%s/%s", operation, strings.Join(trace, "-")), func(t *testing.T) {
					runLifecycleBarrierTrace(t, operation, trace)
				})
			}
		}
	}
}

func runLifecycleBarrierTrace(t *testing.T, operation string, trace []string) {
	t.Helper()
	store := &incarnationWaitState{
		State: state.WrapCore(namespaced.NewState(inmem.Build)), waiting: make(chan struct{}),
	}
	r, s, finishSetup := lifecycleTestBlockedSetupWithState(t, store)
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	finishAdmission := holdJournalBatch(t, ctx, s)
	admitted := make(chan error, 1)
	go func() {
		_, err := r.SubmitInput(sessionCallContext(r, ctx), &environmentruntimepb.SubmitInputRequest{
			SessionId: s.ID(), CommandId: "held-input", Text: "input",
		})
		admitted <- err
	}()
	if err := watchSessionResource(ctx, r.lifecycleState, lifecycle.EnvironmentResourceID, func(input environmentAdmissionCountSpec) (bool, error) {
		return input.Admissions == 1, nil
	}); err != nil {
		t.Fatalf("wait for admission: %v", err)
	}
	staleCursor := s.EventJournal().LastCursor()
	if _, err := r.Quiesce(ctx, &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatal(err)
	}
	waitLifecycle(t, r, func() bool {
		current := currentSessionLifecycleStatus(r)
		return current.Stage == lifecycle.Draining && current.Blocker.Kind == lifecycle.AdmissionsPending
	}, "quiesce admission barrier")
	blocker := sessionLifecycleSnapshot(t, r).Blocker
	if blocker == nil || blocker.ResourceType != lifecycle.AdmissionCountType || blocker.ResourceId != lifecycle.EnvironmentResourceID {
		t.Fatalf("externally reported admission blocker = %+v; want Runtime resource identity", blocker)
	}

	released := map[string]bool{}
	var completed chan error
	for _, event := range trace {
		switch event {
		case "admission":
			finishAdmission()
			select {
			case err := <-admitted:
				if status.Code(err) != codes.Aborted {
					t.Fatalf("late admission = %v, want Aborted", err)
				}
			case <-ctx.Done():
				t.Fatal("admission did not settle")
			}
		case "setup":
			finishSetup()
		case "close":
			completed = make(chan error, 1)
			go func() { completed <- r.Close(context.Background()) }()
			if !released["admission"] || !released["setup"] {
				waitLifecycle(t, r, func() bool {
					return environmentDesired(r).Mode == lifecycle.Closing
				}, "Close intent while work remains")
			}
		case "replace":
			if !released["admission"] || !released["setup"] {
				replaceCtx, stop := context.WithCancel(ctx)
				stop()
				if err := r.adoptIncarnation(replaceCtx, "replacement-incarnation"); err == nil {
					t.Fatal("canceled replacement overtook native work")
				}
			}
		case "acknowledge":
			ack := retained.Acknowledgement{Cursor: staleCursor, Sealed: true}
			if err := r.store.WriteAcknowledgement(s.ID(), ack); err != nil {
				t.Fatal(err)
			}
		}
		released[event] = true
		if released["admission"] && released["setup"] {
			continue
		}
		if seal := s.EventJournal().SealState(); seal != nil {
			t.Fatalf("%v after %s: sealed before work settled: %+v", trace, event, seal)
		}
		if sessionAdmissionOpen(r) {
			t.Fatalf("%v after %s: reopened admission before work settled", trace, event)
		}
		select {
		case err := <-completed:
			t.Fatalf("%v after %s: lifecycle operation overtook work: %v", trace, event, err)
		default:
		}
	}

	if operation == "acknowledge" {
		waitLifecycle(t, r, func() bool {
			current := currentSessionLifecycleStatus(r)
			return current.Stage == lifecycle.Flushing && current.Blocker.AcknowledgementPresent &&
				current.Blocker.AcknowledgementCursor == staleCursor
		}, "stale acknowledgement to retain the flushing barrier")
		seal := s.EventJournal().SealState()
		if seal == nil || seal.Cursor <= staleCursor || seal.IncarnationUID != "test-incarnation" {
			t.Fatalf("seal after setup = %+v, stale cursor = %d", seal, staleCursor)
		}
		ack := retained.Acknowledgement{Cursor: seal.Cursor, Sealed: true}
		if err := r.store.WriteAcknowledgement(s.ID(), ack); err != nil {
			t.Fatal(err)
		}
		waitLifecycle(t, r, func() bool { return currentSessionLifecycleStatus(r).Stage == lifecycle.Quiesced }, "exact acknowledgement")
		return
	}
	if operation == "replace" {
		if err := r.waitSessionStage(ctx, lifecycle.Flushing); err != nil {
			t.Fatal(err)
		}
		seal := s.EventJournal().SealState()
		if seal == nil {
			t.Fatal("replacement has no prior seal")
		}
		if err := r.store.WriteAcknowledgement(s.ID(), retained.Acknowledgement{Cursor: seal.Cursor, Sealed: true}); err != nil {
			t.Fatal(err)
		}
		if err := r.adoptIncarnation(ctx, "replacement-incarnation"); err != nil {
			t.Fatal(err)
		}
		if r.environment().IncarnationUID != "replacement-incarnation" || !sessionAdmissionOpen(r) {
			t.Fatal("replacement binding did not reopen")
		}
		if s.EventJournal().SealState() != nil {
			t.Fatal("replacement did not reopen journal")
		}
		return
	}
	waitLifecycle(t, r, func() bool { return s.EventJournal().SealState() != nil }, "Close to seal after setup")
	seal := s.EventJournal().SealState()
	if err := r.store.WriteAcknowledgement(s.ID(), retained.Acknowledgement{Cursor: seal.Cursor, Sealed: true}); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-completed:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal("close did not settle")
	}
}

// A valid old Gateway RPC has reached the receiver but has not entered Runtime
// admission when the managed-deletion quiescence barrier completes.
func TestForwardedInterruptCannotAdmitAfterQuiescedBoundary(t *testing.T) {
	r, s := lifecycleTestSession(t)
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	entered, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	releaseCall := func() { once.Do(func() { close(release) }) }
	t.Cleanup(releaseCall)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.UnaryInterceptor(func(ctx context.Context, request any, _ *grpc.UnaryServerInfo, next grpc.UnaryHandler) (any, error) {
		if interrupt, ok := request.(*environmentruntimepb.InterruptTurnRequest); ok && interrupt.GetCommandId() == "delayed-interrupt" {
			close(entered)
			select {
			case <-release:
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		return next(ctx, request)
	}))
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, r.control)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	incoming, _ := metadata.FromIncomingContext(sessionCallContext(r, ctx))
	callCtx := metadata.NewOutgoingContext(ctx, incoming.Copy())
	client := environmentruntimepb.NewEnvironmentRuntimeClient(conn)
	prior := &environmentruntimepb.ChangeModelRequest{SessionId: s.ID(), CommandId: "before-deletion", Model: "test-model"}
	receipt, err := client.ChangeModel(callCtx, prior)
	if err != nil {
		t.Fatalf("pre-boundary admission: %v", err)
	}
	delayed := make(chan error, 1)
	go func() {
		_, err := client.InterruptTurn(callCtx, &environmentruntimepb.InterruptTurnRequest{SessionId: s.ID(), CommandId: "delayed-interrupt", TurnId: "old-turn"})
		delayed <- err
	}()
	select {
	case <-entered:
	case <-ctx.Done():
		t.Fatal("interrupt did not reach receiver")
	}
	if _, err := r.Quiesce(ctx, &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatal(err)
	}
	waitLifecycle(t, r, func() bool { return currentSessionLifecycleStatus(r).Stage == lifecycle.Flushing }, "final journal seal")
	seal := s.EventJournal().SealState()
	if seal == nil {
		t.Fatal("quiescence has no final seal")
	}
	if err := r.store.WriteAcknowledgement(s.ID(), retained.Acknowledgement{Cursor: seal.Cursor, Sealed: true}); err != nil {
		t.Fatal(err)
	}
	waitLifecycle(t, r, func() bool { return currentSessionLifecycleStatus(r).Stage == lifecycle.Quiesced }, "managed deletion barrier")
	releaseCall()
	select {
	case err := <-delayed:
		if status.Code(err) != codes.FailedPrecondition {
			t.Fatalf("delayed interrupt = %v, want sealed refusal", err)
		}
	case <-ctx.Done():
		t.Fatal("delayed interrupt did not settle")
	}
	if command, err := s.EventJournal().GetCommand(ctx, "delayed-interrupt"); err != nil || command != nil {
		t.Fatalf("late interrupt entered journal: %+v, %v", command, err)
	}
	retried, err := client.ChangeModel(callCtx, prior)
	if err != nil || retried.GetAdmission().GetCursor() != receipt.GetAdmission().GetCursor() {
		t.Fatalf("pre-boundary receipt retry = %+v, %v", retried, err)
	}
	if s.EventJournal().LastCursor() != seal.Cursor {
		t.Fatal("post-boundary call appended to sealed journal")
	}
}
