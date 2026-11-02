package runtime

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

func TestSealedIncarnationReopensOnlyForItsSuccessor(t *testing.T) {
	proxy := modelConfigServer(t)
	r := newLifecycleRuntime(t, lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false"))
	if _, err := r.Quiesce(t.Context(), &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatal(err)
	}
	if err := r.waitSessionStage(t.Context(), lifecycle.Quiesced); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	sealed := r.desired
	r.mu.Unlock()
	if err := r.adoptIncarnation(t.Context(), "test-incarnation"); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	retained, accepting := r.desired, r.admissionOpen
	r.mu.Unlock()
	if retained != sealed || accepting {
		t.Fatalf("same sealed incarnation reopened: desired=%+v accepting=%t", retained, accepting)
	}
	if err := r.adoptIncarnation(context.Background(), "successor"); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	successor, accepting := r.desired, r.admissionOpen
	r.mu.Unlock()
	if successor.Mode != lifecycle.Running || successor.IncarnationUID != "successor" || successor.SealUID != "" || successor.Generation <= sealed.Generation || !accepting {
		t.Fatalf("successor did not reopen after sealed completion: desired=%+v accepting=%t", successor, accepting)
	}
}
