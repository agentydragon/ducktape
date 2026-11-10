package session

import (
	"context"
	"sync"
	"testing"
	"testing/synctest"

	"github.com/agentydragon/ducktape/agentplane/env/oneoff"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

// Intercept the candidate snapshot inside COSI's retrying update, before commit.
type interceptedSnapshotState struct {
	state.State
	beforeWrite func(old, next ReconcileSnapshot)
}

func (s *interceptedSnapshotState) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	return s.State.Modify(ctx, value, func(current resource.Resource) error {
		snapshot, ok := current.(*typed.Resource[ReconcileSnapshot, lifecycle.Extension[ReconcileSnapshot]])
		var old ReconcileSnapshot
		if ok {
			old = *snapshot.TypedSpec()
		}
		if err := update(current); err != nil {
			return err
		}
		if ok {
			s.beforeWrite(old, *snapshot.TypedSpec())
		}
		return nil
	}, opts...)
}

func operationTestSession(t *testing.T, st state.State) (*Session, *Manager) {
	t.Helper()
	ctx := t.Context()
	fail := func(err error) {
		if err != nil {
			t.Error(err)
		}
	}
	m := &Manager{ctx: ctx, state: st, fail: fail}
	// Suppress native/journal notifications to isolate operation scheduling.
	s := &Session{id: "test-session", loading: true, record: &retained.Record{}, deps: sessionDependencies{
		ctx: ctx, signalOperation: m.signalOperation, publish: m.publishSession, fail: fail, changed: func() {},
	}}
	m.sessions = map[string]*Session{s.id: s}
	m.publishSession(s, false)
	return s, m
}

func TestOperationCompletionPublicationPrecedesConsumption(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		publishing, release := make(chan struct{}), make(chan struct{})
		st := &interceptedSnapshotState{State: state.WrapCore(namespaced.NewState(inmem.Build)), beforeWrite: func(_, next ReconcileSnapshot) {
			if next.OperationRevision == 2 {
				close(publishing)
				<-release
			}
		}}
		s, _ := operationTestSession(t, st)
		op, err := s.startOperation(&s.preparation, func(context.Context) error { return nil })
		if err != nil {
			t.Fatal(err)
		}
		<-publishing
		if op.Finished() {
			t.Fatal("operation became consumable before completion publication")
		}
		if s.mu.TryLock() {
			s.mu.Unlock()
			t.Fatal("completion publication did not protect the operation slot")
		}
		consumed := make(chan bool, 1)
		consumerStarted := make(chan struct{})
		go func() {
			close(consumerStarted)
			_, done := s.takeOperation(&s.preparation)
			consumed <- done
		}()
		<-consumerStarted
		select {
		case <-consumed:
			t.Fatal("consumption did not wait for completion publication")
		default:
		}
		close(release)
		if !<-consumed {
			t.Fatal("controller did not consume after completion publication")
		}
		s.work.Wait()
		if _, done := s.takeOperation(&s.preparation); done {
			t.Fatal("operation consumed twice")
		}
	})
}

func TestOperationSignalsWakeUnchangedSnapshotWithoutExecutionDemand(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		st := state.WrapCore(namespaced.NewState(inmem.Build))
		s, m := operationTestSession(t, st)
		events := make(chan state.Event, 8)
		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()
		if err := st.Watch(ctx, lifecycle.ResourceFor(s.id, ReconcileSnapshot{}).Metadata(), events); err != nil {
			t.Fatal(err)
		}
		<-events // Initial snapshot.
		m.publishSession(s, false)
		synctest.Wait()
		select {
		case event := <-events:
			t.Fatalf("unchanged snapshot produced a watch event: %v", event.Type)
		default:
		}
		release := make(chan struct{})
		op, err := s.startOperation(&s.preparation, func(context.Context) error { <-release; return nil })
		if err != nil {
			t.Fatal(err)
		}
		for revision := uint64(1); revision <= 2; revision++ {
			event := <-events
			input := event.Resource.(*typed.Resource[ReconcileSnapshot, lifecycle.Extension[ReconcileSnapshot]]).TypedSpec()
			if event.Type != state.Updated || input.OperationRevision != revision || input.Demand != 0 {
				t.Fatalf("operation signal: event=%v snapshot=%+v", event.Type, input)
			}
			if revision == 1 {
				close(release)
			}
		}
		<-op.Done()
		s.work.Wait()
	})
}

func TestConcurrentOperationCompletionsPreserveSnapshotRevision(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		st := state.WrapCore(namespaced.NewState(inmem.Build))
		s, m := operationTestSession(t, st)
		release := make(chan struct{})
		for _, slot := range []**oneoff.Operation{&s.preparation, &s.inspection, &s.dispatching} {
			if _, err := s.startOperation(slot, func(context.Context) error { <-release; return nil }); err != nil {
				t.Fatal(err)
			}
		}
		var writers sync.WaitGroup
		writers.Go(func() {
			for range 20 {
				m.publishSession(s, true)
			}
		})
		writers.Go(func() {
			for range 20 {
				if err := m.ResetDemand(); err != nil {
					t.Error(err)
				}
			}
		})
		close(release)
		s.work.Wait()
		writers.Wait()
		input, _, err := ReadReconcileSnapshot(t.Context(), st, s.id)
		if err != nil || input.OperationRevision != 6 {
			t.Fatalf("concurrent snapshot writes lost operation signals: snapshot=%+v err=%v", input, err)
		}
		for _, slot := range []**oneoff.Operation{&s.preparation, &s.inspection, &s.dispatching} {
			if op, done := s.takeOperation(slot); !done || op.Err() != nil {
				t.Fatalf("operation did not settle: op=%v done=%t", op, done)
			}
		}
	})
}

func TestSnapshotConflictPreservesOperationCompletion(t *testing.T) {
	for _, reset := range []bool{false, true} {
		t.Run(map[bool]string{false: "snapshot", true: "demand-reset"}[reset], func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				publishing, release := make(chan struct{}), make(chan struct{})
				var intercept sync.Once
				st := &interceptedSnapshotState{State: state.WrapCore(namespaced.NewState(inmem.Build)), beforeWrite: func(old, next ReconcileSnapshot) {
					if old.Demand != next.Demand {
						intercept.Do(func() { close(publishing); <-release })
					}
				}}
				s, m := operationTestSession(t, st)
				if reset {
					if err := lifecycle.Publish(t.Context(), st.State, s.id, ReconcileSnapshot{Demand: 1}, lifecycle.EnvironmentOwner); err != nil {
						t.Fatal(err)
					}
				}
				written := make(chan struct{})
				go func() {
					defer close(written)
					if reset {
						if err := m.ResetDemand(); err != nil {
							t.Error(err)
						}
					} else {
						m.publishSession(s, true)
					}
				}()
				<-publishing
				if err := m.signalOperation(s.id); err != nil {
					t.Fatal(err)
				}
				close(release)
				<-written
				input, _, err := ReadReconcileSnapshot(t.Context(), st, s.id)
				if err != nil || input.OperationRevision != 1 {
					t.Fatalf("conflict retry lost completion signal: snapshot=%+v err=%v", input, err)
				}
			})
		})
	}
}
