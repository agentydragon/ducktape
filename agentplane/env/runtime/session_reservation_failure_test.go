package runtime

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// reservationBacking models a write that can commit before returning an error.
// All other resource types are ephemeral, as in the selective Runtime store.
type reservationBacking struct {
	mu      sync.Mutex
	values  map[string]resource.Resource
	fail    bool
	commit  bool
	failure error
}

func (b *reservationBacking) Load(_ context.Context, handler inmem.LoadHandler) error {
	b.mu.Lock()
	values := make([]resource.Resource, 0, len(b.values))
	for _, value := range b.values {
		values = append(values, value.DeepCopy())
	}
	b.mu.Unlock()
	for _, value := range values {
		if err := handler(retained.SessionRecordType, value); err != nil {
			return err
		}
	}
	return nil
}

func (b *reservationBacking) Put(_ context.Context, typ resource.Type, value resource.Resource) error {
	if typ != retained.SessionRecordType {
		return nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.fail || b.commit {
		b.values[value.Metadata().ID()] = value.DeepCopy()
	}
	if b.fail {
		b.fail = false
		return b.failure
	}
	return nil
}

func (b *reservationBacking) Destroy(_ context.Context, typ resource.Type, ptr resource.Pointer) error {
	if typ == retained.SessionRecordType {
		b.mu.Lock()
		delete(b.values, ptr.ID())
		b.mu.Unlock()
	}
	return nil
}

func reservationState(backing *reservationBacking) state.State {
	build := inmem.NewStateWithOptions(inmem.WithBackingStore(backing))
	return state.WrapCore(namespaced.NewState(func(ns resource.Namespace) state.CoreState { return build(ns) }))
}

func TestUncertainSessionReservationFailsRuntimeUntilRestart(t *testing.T) {
	for _, committed := range []bool{false, true} {
		t.Run(fmt.Sprintf("committed=%t", committed), func(t *testing.T) {
			proxy := modelConfigServer(t)
			workspace := t.TempDir()
			config := lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false")
			config.expectCloseError = true
			failure := errors.New("injected uncertain write")
			backing := &reservationBacking{values: map[string]resource.Resource{}, fail: true, commit: committed, failure: failure}
			r := newSessionTestRuntimeWithState(t, config, reservationState(backing))
			request := openLifecycleSession("uncertain", filepath.Join(workspace, "project"))
			marker := filepath.Join(t.TempDir(), "setup-ran")
			script := fmt.Sprintf("touch %s", shellQuote(marker))
			request.SetupScript = &script
			ctx := sessionCallContext(r, t.Context())
			if _, err := r.CreateSession(ctx, request); status.Code(err) != codes.Unavailable {
				t.Fatalf("first OpenSession = %v, want Unavailable", err)
			}
			select {
			case err := <-r.fatal:
				if !errors.Is(err, failure) {
					t.Fatalf("fatal error = %v", err)
				}
			default:
				t.Fatal("uncertain write did not fail Runtime")
			}
			if _, err := r.CreateSession(ctx, request); status.Code(err) != codes.Unavailable {
				t.Fatalf("retry before restart = %v, want Unavailable", err)
			}
			conflict := openLifecycleSession(request.SessionId, filepath.Join(workspace, "project"))
			conflict.Spec.Model = "other-model"
			if _, err := r.CreateSession(ctx, conflict); status.Code(err) != codes.Unavailable {
				t.Fatalf("conflicting retry before restart = %v, want Unavailable", err)
			}
			backing.mu.Lock()
			var originalID string
			if value := backing.values[request.SessionId]; value != nil {
				originalID = value.(*retained.SessionRecord).TypedSpec().Value.EventSourceId
			}
			backing.mu.Unlock()
			if committed && originalID == "" {
				t.Fatal("committed reservation has no event source")
			}
			if err := closeRuntimeForTest(t, r); !errors.Is(err, failure) {
				t.Fatalf("close failed Runtime = %v, want reservation failure", err)
			}
			// Close deliberately retains the owner when the failed lifecycle cannot
			// prove cleanup. Simulate process exit after joining the test components.
			_ = r.stopSessions(t.Context())
			_ = r.stopIngress(t.Context())
			r.lifecycleCancel()
			<-r.lifecycleDone
			_ = r.closeSessions(t.Context())
			if err := closeTestRuntimeStore(r); err != nil {
				t.Fatal(err)
			}

			config.expectCloseError = false
			recovered := newSessionTestRuntimeWithState(t, config, reservationState(backing))
			recoveredCtx := sessionCallContext(recovered, t.Context())
			if committed {
				if _, err := recovered.CreateSession(recoveredCtx, conflict); status.Code(err) != codes.AlreadyExists {
					t.Fatalf("conflicting retry after restart = %v, want AlreadyExists", err)
				}
				if _, err := recovered.CreateSession(recoveredCtx, request); err != nil {
					t.Fatalf("identical retry after restart: %v", err)
				}
				record, err := recovered.store.Read(request.SessionId)
				if err != nil || record.EventSourceId != originalID {
					t.Fatalf("recovered event source = %v, err=%v; want %s", record, err, originalID)
				}
				input, found, err := session.ReadReconcileSnapshot(recovered.lifecycleContext, recovered.lifecycleState, request.SessionId)
				if err != nil || !found || input.Setup != sessionpb.SetupState_SETUP_STATE_INTERRUPTED {
					t.Fatalf("recovered setup = %+v, found=%t, err=%v", input, found, err)
				}
				if _, err := os.Stat(marker); !errors.Is(err, os.ErrNotExist) {
					t.Fatalf("uncertain setup replayed: %v", err)
				}
			} else {
				// The reload proves no reservation exists, so a fresh request may proceed.
				request.SetupScript = nil
				if _, err := recovered.CreateSession(recoveredCtx, request); err != nil {
					t.Fatalf("fresh open after restart: %v", err)
				}
			}
		})
	}
}
