package harnesshost

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/retained"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type incarnationWaitContextKey struct{}

type incarnationWaitState struct {
	state.State
	waiting chan struct{}
	once    sync.Once
}

func (s *incarnationWaitState) WatchFor(ctx context.Context, pointer resource.Pointer, conditions ...state.WatchForConditionFunc) (resource.Resource, error) {
	if ctx.Value(incarnationWaitContextKey{}) != nil && pointer.Type() == runnerlifecycle.AggregateStatusType {
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
		_, err := r.SubmitInput(hostContext(r, ctx), &environmentruntimepb.SubmitInputRequest{
			SessionId: s.ID(), CommandId: "held-input", Text: "input",
		})
		admitted <- err
	}()
	if err := watchLifecycle(ctx, r.lifecycle, "runner", func(input runnerInputSpec) (bool, error) {
		return input.Admissions == 1, nil
	}); err != nil {
		t.Fatalf("wait for admission: %v", err)
	}
	staleCursor := s.EventJournal().LastCursor()
	if _, err := quiesceHost(ctx, r); err != nil {
		t.Fatal(err)
	}
	waitLifecycle(t, r, func() bool {
		current := r.lifecycle.status()
		return current.Stage == runnerlifecycle.Draining && current.Blocker.Kind == runnerlifecycle.AdmissionsPending
	}, "quiesce admission barrier")

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
					return r.lifecycle.desiredState().Mode == runnerlifecycle.Closing
				}, "Close intent while work remains")
			}
		case "replace":
			if !released["admission"] || !released["setup"] {
				if err := bindHost(r, "replacement-incarnation"); err == nil {
					t.Fatal("binding replaced while native work remained")
				}
			}
		case "acknowledge":
			ack := retained.Acknowledgement{Cursor: staleCursor, Sealed: true}
			if err := r.store.WriteAcknowledgement(s.ID(), ack); err != nil {
				t.Fatal(err)
			}
			if err := r.lifecycle.publishAck(s.ID(), ack); err != nil {
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
		if r.lifecycle.accepting() {
			t.Fatalf("%v after %s: reopened admission before work settled", trace, event)
		}
		select {
		case err := <-completed:
			t.Fatalf("%v after %s: lifecycle operation overtook work: %v", trace, event, err)
		default:
		}
		owner, err := retained.OpenOwner(filepath.Join(r.config.StateDir, "harness-host"))
		if err == nil {
			_ = owner.Close()
			t.Fatalf("%v after %s: released state ownership before work settled", trace, event)
		}
	}

	if operation == "acknowledge" {
		waitLifecycle(t, r, func() bool {
			current := r.lifecycle.status()
			return current.Stage == runnerlifecycle.Flushing && current.Blocker.AcknowledgementPresent &&
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
		if err := r.lifecycle.publishAck(s.ID(), ack); err != nil {
			t.Fatal(err)
		}
		waitLifecycle(t, r, func() bool { return r.lifecycle.status().Stage == runnerlifecycle.Quiesced }, "exact acknowledgement")
		return
	}
	if operation == "replace" {
		if err := r.lifecycle.wait(ctx, runnerlifecycle.Flushing); err != nil {
			t.Fatal(err)
		}
		if err := bindHost(r, "replacement-incarnation"); err != nil {
			t.Fatal(err)
		}
		if r.environment().IncarnationUID != "replacement-incarnation" || !r.lifecycle.accepting() {
			t.Fatal("replacement binding did not reopen")
		}
		if s.EventJournal().SealState() != nil {
			t.Fatal("replacement did not reopen journal")
		}
		return
	}
	select {
	case err := <-completed:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal("close did not settle")
	}
	// Transport-loss/local cleanup cannot attest an Execution fence or manufacture a seal.
	owner, err := retained.OpenOwner(filepath.Join(r.config.StateDir, "harness-host"))
	if err != nil {
		t.Fatalf("Close retained state ownership after cleanup: %v", err)
	}
	_ = owner.Close()
}
