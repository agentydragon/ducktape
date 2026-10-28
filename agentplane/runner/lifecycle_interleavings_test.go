package runner

import (
	"context"
	"fmt"
	"strings"
	"testing"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

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
	r, s, finishSetup := lifecycleTestBlockedSetup(t)
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	finishAdmission := holdJournalBatch(t, ctx, s)
	admitted := make(chan error, 1)
	go func() {
		_, err := r.SubmitInput(ctx, &runnerservicepb.SubmitInputRequest{
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
	if _, err := r.Quiesce(ctx, &runnerpb.QuiesceRequest{}); err != nil {
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
			go func() { completed <- r.Close() }()
			if !released["admission"] || !released["setup"] {
				waitLifecycle(t, r, func() bool {
					return r.lifecycle.desiredState().Mode == runnerlifecycle.Closing
				}, "Close intent while work remains")
			}
		case "replace":
			completed = make(chan error, 1)
			go func() { completed <- r.adoptIncarnation(ctx, "replacement-incarnation") }()
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
		owner, err := retained.OpenOwner(r.config.StateDir)
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
	select {
	case err := <-completed:
		if err != nil {
			t.Fatalf("%s after work settled: %v", operation, err)
		}
	case <-ctx.Done():
		t.Fatalf("%s waited for ingress acknowledgement", operation)
	}
	if operation == "replace" {
		if uid, known := r.store.IncarnationUID(); !known || uid != "replacement-incarnation" {
			t.Fatalf("replacement incarnation = %q, known = %v", uid, known)
		}
		if seal := s.EventJournal().SealState(); seal != nil || !r.lifecycle.accepting() {
			t.Fatalf("replacement did not reopen the sealed Session: seal=%+v", seal)
		}
		return
	}
	if seal := s.EventJournal().SealState(); seal == nil || seal.IncarnationUID != "test-incarnation" {
		t.Fatalf("Close lost the accepted quiesce seal: %+v", seal)
	}
	owner, err := retained.OpenOwner(r.config.StateDir)
	if err != nil {
		t.Fatalf("Close retained state ownership after cleanup: %v", err)
	}
	_ = owner.Close()
}
