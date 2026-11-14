package rpc

import (
	"crypto/x509"
	"net"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	notificationtesting "github.com/agentydragon/ducktape/agentplane/notification_service/testing" // Shared gRPC fixtures; avoids colliding with Go testing.
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// serveRunner serves the runner on a loopback port and returns a ProxyRunner whose target is that port.
func serveRunner(t *testing.T, runner *notificationtesting.ListingRunner) *ProxyRunner {
	t.Helper()
	runner.Received = make(chan metadata.MD, 1)
	address := notificationtesting.ServeGRPC(t, func(server *grpc.Server) {
		environmentruntimepb.RegisterEnvironmentRuntimeServer(server, runner)
	})
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("test-proxy-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return &ProxyRunner{TokenFile: tokenFile, Timeout: 10 * time.Second, TargetTemplate: address}
}

var proxiedSandbox = &sandboxreader.Identity{Namespace: "test-namespace", Name: "test-sandbox", Uid: "test-sandbox-uid"}

func TestProxyRunnerListsTheSessionsOfTheSandboxItNames(t *testing.T) {
	runner := &notificationtesting.ListingRunner{Sessions: []string{"first-session", "second-session"}}
	sessions, err := serveRunner(t, runner).ListSessions(t.Context(), proxiedSandbox)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(sessions, []string{"first-session", "second-session"}) {
		t.Errorf("sessions = %v", sessions)
	}
	// The proxy admits the call by the bearer and routes it by the Sandbox's identity.
	received := <-runner.Received
	want := map[string]string{
		"authorization":                                        "Bearer test-proxy-token",
		sandboxapi.RunnerProxySandboxMetadataKey:               "test-sandbox",
		sandboxapi.RunnerProxySandboxNamespaceMetadataKey:      "test-namespace",
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

func TestProxyRunnerUsesNotificationsSVIDWithoutBearerToken(t *testing.T) {
	ca := peertlstesting.NewCA(t)
	serverIdentity := ca.IssueWithUsage(t, peertls.SessionDispatcherURI, x509.ExtKeyUsageServerAuth)
	serverTLS, err := serverIdentity.WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, false)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	runner := &notificationtesting.ListingRunner{Sessions: []string{"svid-session"}, Received: make(chan metadata.MD, 1)}
	server := grpc.NewServer(grpc.Creds(credentials.NewTLS(serverTLS)))
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, runner)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)

	clientIdentity := ca.IssueWithUsage(t, peertls.NotificationsURI, x509.ExtKeyUsageClientAuth)
	proxy := &ProxyRunner{
		TLS: &peertls.ClientTLS{
			CAFile: ca.RootFile, ServerURI: peertls.SessionDispatcherURI,
			CertFile: clientIdentity.CertFile, KeyFile: clientIdentity.KeyFile, ClientURI: peertls.NotificationsURI,
		},
		Timeout: 10 * time.Second, TargetTemplate: listener.Addr().String(),
	}
	sessions, err := proxy.ListSessions(t.Context(), proxiedSandbox)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(sessions, []string{"svid-session"}) {
		t.Fatalf("sessions = %v", sessions)
	}
	if got := (<-runner.Received).Get("authorization"); len(got) != 0 {
		t.Fatalf("authorization metadata = %v, want no bearer fallback", got)
	}
}

func TestProxyRunnerFailsWhatTheRunnerOrTheTokenFails(t *testing.T) {
	t.Run("runner error", func(t *testing.T) {
		runner := &notificationtesting.ListingRunner{Err: status.Error(codes.Unavailable, "runner is draining")}
		_, err := serveRunner(t, runner).ListSessions(t.Context(), proxiedSandbox)
		if got := status.Code(err); got != codes.Unavailable {
			t.Errorf("code = %v (%v), want Unavailable", got, err)
		}
	})
	t.Run("no token file", func(t *testing.T) {
		runner := serveRunner(t, &notificationtesting.ListingRunner{})
		runner.TokenFile = filepath.Join(t.TempDir(), "absent")
		if _, err := runner.ListSessions(t.Context(), proxiedSandbox); err == nil {
			t.Error("a call went out without a token")
		}
	})
}
