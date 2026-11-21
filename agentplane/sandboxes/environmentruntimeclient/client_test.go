package environmentruntimeclient

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

// recordingRuntime is a fake Environment Runtime server that records each call's metadata.
type recordingRuntime struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	received chan metadata.MD
}

func (r *recordingRuntime) GetStatus(ctx context.Context, _ *sessionpb.GetStatusRequest) (*sessionpb.EnvironmentRuntimeStatus, error) {
	incoming, _ := metadata.FromIncomingContext(ctx)
	r.received <- incoming
	return &sessionpb.EnvironmentRuntimeStatus{}, nil
}

// serve starts the recording Environment Runtime server on loopback and returns its Config and token file.
func serve(t *testing.T, token string) (Config, *recordingRuntime) {
	t.Helper()
	runtime := &recordingRuntime{received: make(chan metadata.MD, 4)}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, runtime)
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
	}, runtime
}

func writeToken(t *testing.T, path, token string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(token), 0o600); err != nil {
		t.Fatal(err)
	}
}

func getStatus(ctx context.Context, config Config) error {
	return WithClient(ctx, config, func(ctx context.Context, client environmentruntimepb.EnvironmentRuntimeClient) error {
		_, err := client.GetStatus(ctx, &sessionpb.GetStatusRequest{})
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
	config, runtime := serve(t, "test-token-one\n")
	config.ExpectedIncarnationUID = "test-incarnation-uid"
	for _, want := range []string{"Bearer test-token-one", "Bearer test-token-rotated"} {
		if err := getStatus(t.Context(), config); err != nil {
			t.Fatal(err)
		}
		received := <-runtime.received
		if got := single(t, received, "authorization"); got != want {
			t.Errorf("authorization = %q, want %q", got, want)
		}
		for key, want := range map[string]string{
			sandboxapi.RuntimeProxySandboxMetadataKey:               "test-sandbox",
			sandboxapi.RuntimeProxySandboxNamespaceMetadataKey:      "test-namespace",
			sandboxapi.RuntimeProxySandboxUIDMetadataKey:            "test-sandbox-uid",
			sandboxapi.RuntimeProxySandboxOwnerNamespaceMetadataKey: "test-namespace",
			sandboxapi.RuntimeProxySandboxOwnerNameMetadataKey:      "test-sandbox",
			sandboxapi.RuntimeProxyIncarnationUIDMetadataKey:        "test-incarnation-uid",
		} {
			if got := single(t, received, key); got != want {
				t.Errorf("%s = %q, want %q", key, got, want)
			}
		}
		// The kubelet rotates the projected token in place; the next call carries the new one.
		writeToken(t, config.TokenFile, "test-token-rotated")
	}
}

func TestCallerMetadataIsKeptAndIncarnationUIDIsOptional(t *testing.T) {
	config, runtime := serve(t, "test-token")
	ctx := metadata.AppendToOutgoingContext(t.Context(), "test-caller-key", "test-caller-value")
	if err := getStatus(ctx, config); err != nil {
		t.Fatal(err)
	}
	received := <-runtime.received
	if got := single(t, received, "test-caller-key"); got != "test-caller-value" {
		t.Errorf("caller metadata = %q, want it forwarded", got)
	}
	if got := single(t, received, "authorization"); got != "Bearer test-token" {
		t.Errorf("authorization = %q", got)
	}
	if values := received.Get(sandboxapi.RuntimeProxyIncarnationUIDMetadataKey); len(values) != 0 {
		t.Errorf("optional incarnation UID metadata = %q, want omitted", values)
	}
}

func TestExpandTarget(t *testing.T) {
	template := sandboxapi.RuntimeProxyTargetTemplate()
	if got := ExpandTarget(template, "one", "agentplane-staging"); got != "environments.agentplane-staging.svc:8082" {
		t.Fatalf("target = %q", got)
	}
}
