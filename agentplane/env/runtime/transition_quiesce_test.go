package runtime

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type blockedHostDesiredState struct {
	state.State
	armed   atomic.Bool
	entered chan struct{}
	release chan struct{}
}

func (s *blockedHostDesiredState) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if value.Metadata().Type() == lifecycle.DesiredStateType && s.armed.CompareAndSwap(true, false) {
		close(s.entered)
		select {
		case <-s.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return s.State.Modify(ctx, value, update, opts...)
}

func TestQuiesceStatusRetryRequiresExactCurrentHostGeneration(t *testing.T) {
	binding := &runtimepb.Binding{Generation: 7}
	for _, test := range []struct {
		name          string
		desired       lifecycle.Desired
		desiredFound  bool
		progress      lifecycle.Status
		progressFound bool
		want          bool
	}{
		{name: "current error", desired: lifecycle.Desired{Generation: 7}, desiredFound: true, progress: lifecycle.Status{Generation: 7, Error: "failed"}, progressFound: true, want: true},
		{name: "current healthy", desired: lifecycle.Desired{Generation: 7}, desiredFound: true, progress: lifecycle.Status{Generation: 7}, progressFound: true},
		{name: "missing desired", progress: lifecycle.Status{Generation: 7, Error: "failed"}, progressFound: true},
		{name: "missing progress", desired: lifecycle.Desired{Generation: 7}, desiredFound: true},
		{name: "stale progress error", desired: lifecycle.Desired{Generation: 7}, desiredFound: true, progress: lifecycle.Status{Generation: 6, Error: "failed"}, progressFound: true},
		{name: "newer desired and progress", desired: lifecycle.Desired{Generation: 8}, desiredFound: true, progress: lifecycle.Status{Generation: 8, Error: "failed"}, progressFound: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			got := quiesceStatusNeedsRetry(binding, test.desired, test.desiredFound, test.progress, test.progressFound)
			if got != test.want {
				t.Fatalf("retry = %t, want %t", got, test.want)
			}
		})
	}
}

func TestQuiesceStatusBarrierRequiresConnectedCurrentHostBinding(t *testing.T) {
	proxy := modelConfigServer(t)
	r := newLifecycleRuntime(t, lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false"))
	r.mu.Lock()
	host := r.host
	binding := proto.Clone(r.intent.Binding).(*runtimepb.Binding)
	r.mu.Unlock()
	c := &runtimeTransitionSequence{r: r, quiesce: quiescePlan{host: host, binding: binding, currentReport: true}}
	if _, err := c.runQuiesceBarrier(context.Background(), quiesceCheckHostStatus); err != nil {
		t.Fatalf("current Host status binding = %v", err)
	}
	binding.Generation--
	if _, err := c.runQuiesceBarrier(context.Background(), quiesceCheckHostStatus); status.Code(err) != codes.Unavailable {
		t.Fatalf("stale Host status binding = %v, want unavailable", err)
	}
	binding.Generation++
	host.client.SetRuntimeConnection(r.bootID, host.bootID, false)
	defer host.client.SetRuntimeConnection(r.bootID, host.bootID, true)
	if _, err := c.runQuiesceBarrier(context.Background(), quiesceCheckHostStatus); status.Code(err) != codes.Unavailable {
		t.Fatalf("revoked Host connection gate = %v, want unavailable", err)
	}
	host.client.SetRuntimeConnection(r.bootID, host.bootID, true)
	r.mu.Lock()
	host.connected = false
	r.mu.Unlock()
	t.Cleanup(func() {
		r.mu.Lock()
		host.connected = true
		r.mu.Unlock()
	})
	if _, err := c.runQuiesceBarrier(context.Background(), quiesceCheckHostStatus); status.Code(err) != codes.Unavailable {
		t.Fatalf("disconnected Host status binding = %v, want unavailable", err)
	}
}

func TestQuiesceStatusBarrierDoesNotWaitForHostIntentApplication(t *testing.T) {
	store := &blockedHostDesiredState{
		State:   state.WrapCore(namespaced.NewState(inmem.Build)),
		entered: make(chan struct{}),
		release: make(chan struct{}),
	}
	proxy := modelConfigServer(t)
	r := newLifecycleRunnerWithState(t, lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false"), store)
	r.mu.Lock()
	host := r.host
	intent := proto.Clone(r.intent).(*runtimepb.LifecycleIntent)
	binding := proto.Clone(intent.Binding).(*runtimepb.Binding)
	r.mu.Unlock()
	c := &runtimeTransitionSequence{r: r, quiesce: quiescePlan{host: host, binding: binding, currentReport: true}}
	var release sync.Once
	t.Cleanup(func() { release.Do(func() { close(store.release) }) })
	store.armed.Store(true)
	applied := make(chan error, 1)
	go func() { applied <- host.client.ApplyIntent(t.Context(), intent) }()
	select {
	case <-store.entered:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Host did not reach desired-state publication")
	}
	// ApplyIntent has cleared admission readiness and is holding the Host mutex
	// at this point. The status barrier must read the shared COSI state without
	// waiting for Host admission to reopen.
	checked := make(chan error, 1)
	go func() {
		_, err := c.runQuiesceBarrier(t.Context(), quiesceCheckHostStatus)
		checked <- err
	}()
	select {
	case err := <-checked:
		if err != nil {
			t.Fatalf("current COSI Host status during intent application = %v", err)
		}
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("COSI Host status waited for blocked Host intent application")
	}
	release.Do(func() { close(store.release) })
	select {
	case err := <-applied:
		if err != nil {
			t.Fatalf("reapply Host intent: %v", err)
		}
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Host intent did not finish after release")
	}
}
