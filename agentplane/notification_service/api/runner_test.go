package api

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// listingRunner is a Sandbox's runner as the control Service reaches it.
type listingRunner struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	sessions []string
	err      error
	received chan metadata.MD
}

func (r *listingRunner) ListSessions(ctx context.Context, _ *sessionpb.ListSessionsRequest) (*sessionpb.ListSessionsResponse, error) {
	incoming, _ := metadata.FromIncomingContext(ctx)
	r.received <- incoming
	if r.err != nil {
		return nil, r.err
	}
	response := &sessionpb.ListSessionsResponse{}
	for _, id := range r.sessions {
		response.Sessions = append(response.Sessions, &sessionpb.SessionSummary{SessionId: id})
	}
	return response, nil
}

// serveRunner serves the runner on a loopback port and returns a ProxyRunner whose target is that port.
func serveRunner(t *testing.T, runner *listingRunner) *ProxyRunner {
	t.Helper()
	runner.received = make(chan metadata.MD, 1)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, runner)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("test-proxy-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return &ProxyRunner{TokenFile: tokenFile, Timeout: 10 * time.Second, TargetTemplate: listener.Addr().String()}
}

var proxiedSandbox = sandboxreader.Identity{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-sandbox-uid"}

func TestProxyRunnerListsTheSessionsOfTheSandboxItNames(t *testing.T) {
	runner := &listingRunner{sessions: []string{"first-session", "second-session"}}
	sessions, err := serveRunner(t, runner).ListSessions(t.Context(), proxiedSandbox)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(sessions, []string{"first-session", "second-session"}) {
		t.Errorf("sessions = %v", sessions)
	}
	// The proxy admits the call by the bearer and routes it by the Sandbox's identity.
	received := <-runner.received
	want := map[string]string{
		"authorization":                                        "Bearer test-proxy-token",
		sandboxapi.RunnerProxySandboxMetadataKey:               "test-sandbox",
		sandboxapi.RunnerProxySandboxUIDMetadataKey:            "test-sandbox-uid",
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey: "test-namespace",
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey:      "test-sandbox",
	}
	for key, value := range want {
		if got := received.Get(key); !slices.Equal(got, []string{value}) {
			t.Errorf("%s = %v, want %q", key, got, value)
		}
	}
}

func TestProxyRunnerFailsWhatTheRunnerOrTheTokenFails(t *testing.T) {
	t.Run("runner error", func(t *testing.T) {
		runner := &listingRunner{err: status.Error(codes.Unavailable, "runner is draining")}
		_, err := serveRunner(t, runner).ListSessions(t.Context(), proxiedSandbox)
		if got := status.Code(err); got != codes.Unavailable {
			t.Errorf("code = %v (%v), want Unavailable", got, err)
		}
	})
	t.Run("no token file", func(t *testing.T) {
		runner := serveRunner(t, &listingRunner{})
		runner.TokenFile = filepath.Join(t.TempDir(), "absent")
		if _, err := runner.ListSessions(t.Context(), proxiedSandbox); err == nil {
			t.Error("a call went out without a token")
		}
	})
}
