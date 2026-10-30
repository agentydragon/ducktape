package runnerclient

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

// recordingRunner is the Runner Gateway proxy as WithClient reaches it; it records each call's metadata.
type recordingRunner struct {
	runnerservicepb.UnimplementedRunnerServer
	received chan metadata.MD
}

func (r *recordingRunner) ListSessions(ctx context.Context, _ *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
	incoming, _ := metadata.FromIncomingContext(ctx)
	r.received <- incoming
	return &runnerpb.ListSessionsResponse{}, nil
}

// serve starts the recording runner on loopback and returns a Config for it whose token file holds token.
func serve(t *testing.T, token string) (Config, *recordingRunner) {
	t.Helper()
	runner := &recordingRunner{received: make(chan metadata.MD, 4)}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	runnerservicepb.RegisterRunnerServer(server, runner)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	tokenFile := filepath.Join(t.TempDir(), "token")
	writeToken(t, tokenFile, token)
	return Config{
		Target:           listener.Addr().String(),
		TokenFile:        tokenFile,
		Timeout:          10 * time.Second,
		SandboxName:      "test-sandbox",
		SandboxUID:       "test-sandbox-uid",
		SandboxNamespace: "test-namespace",
	}, runner
}

func writeToken(t *testing.T, path, token string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(token), 0o600); err != nil {
		t.Fatal(err)
	}
}

func listSessions(ctx context.Context, config Config) error {
	return WithClient(ctx, config, func(ctx context.Context, client runnerservicepb.RunnerClient) error {
		_, err := client.ListSessions(ctx, &runnerpb.ListSessionsRequest{})
		return err
	})
}

func single(t *testing.T, md metadata.MD, key string) string {
	t.Helper()
	values := md.Get(key)
	if len(values) != 1 {
		t.Fatalf("%s = %q, want one value", key, values)
	}
	return values[0]
}

func TestServerSeesTheCurrentTokenAndSandboxIdentity(t *testing.T) {
	config, runner := serve(t, "test-token-one\n")
	for _, want := range []string{"Bearer test-token-one", "Bearer test-token-rotated"} {
		if err := listSessions(t.Context(), config); err != nil {
			t.Fatal(err)
		}
		received := <-runner.received
		if got := single(t, received, "authorization"); got != want {
			t.Errorf("authorization = %q, want %q", got, want)
		}
		for key, want := range map[string]string{
			sandboxapi.RunnerProxySandboxMetadataKey:               "test-sandbox",
			sandboxapi.RunnerProxySandboxUIDMetadataKey:            "test-sandbox-uid",
			sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey: "test-namespace",
			sandboxapi.RunnerProxySandboxOwnerNameMetadataKey:      "test-sandbox",
		} {
			if got := single(t, received, key); got != want {
				t.Errorf("%s = %q, want %q", key, got, want)
			}
		}
		// The kubelet rotates the projected token in place; the next call carries the new one.
		writeToken(t, config.TokenFile, "test-token-rotated")
	}
}

func TestReplaceOutgoingMetadataDropsTheCallersMetadata(t *testing.T) {
	for _, replace := range []bool{false, true} {
		config, runner := serve(t, "test-token")
		config.ReplaceOutgoingMetadata = replace
		ctx := metadata.AppendToOutgoingContext(t.Context(), "test-caller-key", "test-caller-value")
		if err := listSessions(ctx, config); err != nil {
			t.Fatal(err)
		}
		received := <-runner.received
		if forwarded := len(received.Get("test-caller-key")) > 0; forwarded == replace {
			t.Errorf("ReplaceOutgoingMetadata=%v forwarded caller metadata=%v", replace, forwarded)
		}
		if got := single(t, received, "authorization"); got != "Bearer test-token" {
			t.Errorf("authorization = %q", got)
		}
	}
}

func TestExpandTarget(t *testing.T) {
	template := sandboxapi.ControlServiceTargetTemplate()
	if got := ExpandTarget(template, "one", "agentplane-staging"); got != "one-control.agentplane-staging.svc:8080" {
		t.Fatalf("target = %q", got)
	}
}
