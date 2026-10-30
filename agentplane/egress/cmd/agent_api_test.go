package main

import (
	"context"
	"io"
	"net"
	"net/http"
	"testing"
	"testing/fstest"

	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/protobuf/encoding/protojson"
)

// The facade is the listener's own client: its gRPC call must come back to the gRPC server mounted
// on the listener it was sent from.
func TestAgentAPIAnswersTheRulesFacadeThroughItsOwnListener(t *testing.T) {
	hop, err := auth.NewResolver(api.TokenReviews(), testAudience, []string{"test-ns"})
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	server, err := newAgentAPI(ctx, listener.Addr().String(), identity.NewVerifier(hop, nil), policy.NewIndex(),
		fstest.MapFS{"llms.txt": {Data: []byte("# index")}}, []byte("{}"))
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { _ = server.Close() })

	get := func(path, authorization string) (int, []byte) {
		t.Helper()
		request, err := http.NewRequest(http.MethodGet, "http://"+listener.Addr().String()+path, nil)
		if err != nil {
			t.Fatal(err)
		}
		if authorization != "" {
			request.Header.Set("Authorization", authorization)
		}
		response, err := http.DefaultClient.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		body, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		return response.StatusCode, body
	}

	code, body := get("/v1/rules", "Bearer "+testToken)
	var rules egressrulespb.GetRulesResponse
	if err := protojson.Unmarshal(body, &rules); err != nil || code != http.StatusOK ||
		rules.GetSubject().GetNamespace() != "test-ns" || rules.GetSubject().GetName() != "test-app" {
		t.Errorf("GET /v1/rules = %d %s (%v), want the rules of test-ns/test-app", code, body, err)
	}
	if code, body := get("/v1/rules", "Bearer forged"); code != http.StatusUnauthorized {
		t.Errorf("GET /v1/rules with a forged bearer = %d %s, want 401", code, body)
	}
	if code, body := get("/llms.txt", ""); code != http.StatusOK || string(body) != "# index" {
		t.Errorf("GET /llms.txt = %d %q, want the documentation", code, body)
	}
}
