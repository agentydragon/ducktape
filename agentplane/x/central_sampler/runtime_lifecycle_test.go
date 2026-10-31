package centralsampler

import (
	"context"
	"testing"
	"time"
)

func startRuntimeForTest(t *testing.T, runtime *Runtime) (context.CancelFunc, <-chan error) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- runtime.Run(ctx) }()

	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for {
		runtime.mu.Lock()
		started := runtime.runCtx != nil
		runtime.mu.Unlock()
		if started {
			return cancel, done
		}
		select {
		case <-deadline.C:
			cancel()
			t.Fatal("runtime did not start")
		case <-ticker.C:
		}
	}
}

func assertRuntimeWaitsForGate(t *testing.T, done <-chan error, description string) {
	t.Helper()
	select {
	case err := <-done:
		t.Fatalf("runtime returned before %s completed: %v", description, err)
	case <-time.After(100 * time.Millisecond):
	}
}

func waitRuntimeDone(t *testing.T, done <-chan error) {
	t.Helper()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("runtime returned an error: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("runtime did not stop")
	}
}

func TestRuntimeShutdownWaitsForClaimReturnAndReleasesOwnership(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	runtime := NewRuntime(store, Settings{MaxLiveSessions: 1, RPCTimeoutSeconds: 5, LeaseSeconds: 60})
	claimCommitted := make(chan Owner, 1)
	allowClaimReturn := make(chan struct{})
	runtime.claimLease = func(ctx context.Context, conversationID string, lease time.Duration) (Owner, error) {
		owner, err := store.Claim(ctx, conversationID, lease)
		if err == nil {
			claimCommitted <- owner
			<-allowClaimReturn
		}
		return owner, err
	}

	cancel, done := startRuntimeForTest(t, runtime)
	defer cancel()
	runtime.Wake(session.ID)

	var owner Owner
	select {
	case owner = <-claimCommitted:
	case <-time.After(5 * time.Second):
		t.Fatal("runtime did not commit its lease claim")
	}
	cancel()
	assertRuntimeWaitsForGate(t, done, "the committed claim to return")
	close(allowClaimReturn)
	waitRuntimeDone(t, done)

	newOwner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatalf("fresh claim failed after runtime shutdown: %v", err)
	}
	if newOwner.Generation != owner.Generation+1 {
		t.Fatalf("fresh claim generation=%d, want %d", newOwner.Generation, owner.Generation+1)
	}
	if err := store.Release(t.Context(), newOwner); err != nil {
		t.Fatal(err)
	}
}

func TestRuntimeShutdownWaitsForReleaseReturn(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	runtime := NewRuntime(store, Settings{MaxLiveSessions: 1, RPCTimeoutSeconds: 5, LeaseSeconds: 60})
	releaseCommitted := make(chan Owner, 1)
	allowReleaseReturn := make(chan struct{})
	runtime.releaseLease = func(ctx context.Context, owner Owner) error {
		if err := store.Release(ctx, owner); err != nil {
			return err
		}
		releaseCommitted <- owner
		<-allowReleaseReturn
		return nil
	}

	cancel, done := startRuntimeForTest(t, runtime)
	defer cancel()
	runtime.Wake(session.ID)

	var owner Owner
	select {
	case owner = <-releaseCommitted:
	case <-time.After(5 * time.Second):
		t.Fatal("runtime did not commit its lease release")
	}
	cancel()
	assertRuntimeWaitsForGate(t, done, "the committed release to return")
	close(allowReleaseReturn)
	waitRuntimeDone(t, done)

	newOwner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatalf("fresh claim failed after runtime shutdown: %v", err)
	}
	if newOwner.Generation != owner.Generation+1 {
		t.Fatalf("fresh claim generation=%d, want %d", newOwner.Generation, owner.Generation+1)
	}
	if err := store.Release(t.Context(), newOwner); err != nil {
		t.Fatal(err)
	}
}

func TestRuntimeShutdownKeepsEffectContextAliveUntilDriverSettles(t *testing.T) {
	runtime := NewRuntime(newTestStore(t), Settings{MaxLiveSessions: 1, RPCTimeoutSeconds: 1, LeaseSeconds: 60})
	seconds := 2
	runtime.settings.ShutdownDrainSeconds = &seconds
	cancel, done := startRuntimeForTest(t, runtime)
	runtime.mu.Lock()
	runtime.drivers.Add(1)
	effectCtx := runtime.runCtx
	runtime.mu.Unlock()
	cancel()
	select {
	case <-runtime.draining:
	case <-time.After(time.Second):
		t.Fatal("runtime did not enter drain")
	}
	if err := effectCtx.Err(); err != nil {
		t.Fatalf("in-flight effect was canceled before drain deadline: %v", err)
	}
	assertRuntimeWaitsForGate(t, done, "the in-flight effect to settle")
	runtime.drivers.Done()
	waitRuntimeDone(t, done)
}

func TestRuntimeDrainDeadlineCancelsUnsettledEffect(t *testing.T) {
	runtime := NewRuntime(newTestStore(t), Settings{MaxLiveSessions: 1, RPCTimeoutSeconds: 1, LeaseSeconds: 60})
	seconds := 1
	runtime.settings.ShutdownDrainSeconds = &seconds
	cancel, done := startRuntimeForTest(t, runtime)
	runtime.mu.Lock()
	runtime.drivers.Add(1)
	effectCtx := runtime.runCtx
	runtime.mu.Unlock()
	go func() {
		<-effectCtx.Done()
		runtime.drivers.Done()
	}()
	cancel()
	select {
	case <-effectCtx.Done():
	case <-time.After(3 * time.Second):
		t.Fatal("drain deadline did not cancel unsettled effect")
	}
	waitRuntimeDone(t, done)
}
