package harnesshost

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/harness"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

func TestResolveModelConfigLeavesCodexToItsModelCatalog(t *testing.T) {
	var lookups atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		lookups.Add(1)
		_, _ = fmt.Fprintf(w, `{"model":%q,"total_context_budget_tokens":32768}`, r.URL.Query().Get("model"))
	}))
	defer proxy.Close()
	config := Config{
		Claude:     &harness.LaunchConfig{Binary: "claude", BaseURL: proxy.URL, Token: "test-token"},
		Codex:      &harness.LaunchConfig{Binary: "codex", BaseURL: proxy.URL + "/v1", Token: "test-token"},
		HTTPClient: http.DefaultClient,
	}

	budget, err := config.ResolveModelConfig(context.Background(), sessionpb.Harness_HARNESS_CODEX, "configured-route")
	if err != nil || budget != nil || lookups.Load() != 0 {
		t.Fatalf("Codex budget = %v, err = %v after %d lookups; want none and no lookup", budget, err, lookups.Load())
	}
	budget, err = config.ResolveModelConfig(context.Background(), sessionpb.Harness_HARNESS_CLAUDE, "configured-route")
	if err != nil || budget == nil || *budget != 32768 || lookups.Load() != 1 {
		t.Fatalf("Claude budget = %v, err = %v after %d lookups; want 32768 from one lookup", budget, err, lookups.Load())
	}
}
