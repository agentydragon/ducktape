package runnerclient

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/metadata"
)

func TestWithClientRefreshesTokenAndCreatesConnectionPerCall(t *testing.T) {
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("first-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	config := Config{
		Target:                  "passthrough:///sandbox-control",
		TokenFile:               tokenFile,
		Timeout:                 time.Second,
		SandboxName:             "sandbox",
		SandboxUID:              "uid-sandbox",
		SandboxNamespace:        "agentplane-staging",
		DisableRetry:            true,
		ReplaceOutgoingMetadata: true,
	}
	base := metadata.NewOutgoingContext(context.Background(), metadata.Pairs("trace-id", "caller"))
	var clients []runnerservicepb.RunnerClient
	for _, token := range []string{"first-token", "rotated-token"} {
		if token == "rotated-token" {
			if err := os.WriteFile(tokenFile, []byte(token), 0o600); err != nil {
				t.Fatal(err)
			}
		}
		err := WithClient(base, config, func(ctx context.Context, client runnerservicepb.RunnerClient) error {
			clients = append(clients, client)
			deadline, ok := ctx.Deadline()
			if !ok || time.Until(deadline) <= 0 {
				t.Error("call context has no live deadline")
			}
			md, ok := metadata.FromOutgoingContext(ctx)
			if !ok || len(md.Get("authorization")) != 1 || md.Get("authorization")[0] != "Bearer "+token || md.Get("trace-id") != nil {
				t.Errorf("outgoing metadata = %v", md)
			}
			if len(md.Get(sandboxapi.RunnerProxySandboxMetadataKey)) != 1 || md.Get(sandboxapi.RunnerProxySandboxMetadataKey)[0] != config.SandboxName ||
				len(md.Get(sandboxapi.RunnerProxySandboxUIDMetadataKey)) != 1 || md.Get(sandboxapi.RunnerProxySandboxUIDMetadataKey)[0] != config.SandboxUID ||
				len(md.Get(sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey)) != 1 || md.Get(sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey)[0] != config.SandboxNamespace ||
				len(md.Get(sandboxapi.RunnerProxySandboxOwnerNameMetadataKey)) != 1 || md.Get(sandboxapi.RunnerProxySandboxOwnerNameMetadataKey)[0] != config.SandboxName {
				t.Errorf("Sandbox identity metadata = %v", md)
			}
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	if len(clients) != 2 || clients[0] == clients[1] {
		t.Fatal("calls did not receive fresh connections")
	}
}

func TestWithClientAppendsMetadataWhenConfigured(t *testing.T) {
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("token"), 0o600); err != nil {
		t.Fatal(err)
	}
	config := Config{
		Target:           "passthrough:///sandbox-control",
		TokenFile:        tokenFile,
		Timeout:          time.Second,
		SandboxName:      "sandbox",
		SandboxUID:       "uid-sandbox",
		SandboxNamespace: "agentplane-staging",
	}
	base := metadata.NewOutgoingContext(context.Background(), metadata.Pairs("trace-id", "caller"))
	if err := WithClient(base, config, func(ctx context.Context, _ runnerservicepb.RunnerClient) error {
		md, ok := metadata.FromOutgoingContext(ctx)
		if !ok || len(md.Get("trace-id")) != 1 || md.Get("trace-id")[0] != "caller" {
			t.Errorf("outgoing metadata = %v", md)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}

func TestExpandTarget(t *testing.T) {
	template := sandboxapi.ControlServiceTargetTemplate()
	if got := ExpandTarget(template, "one", "agentplane-staging"); got != "one-control.agentplane-staging.svc:8080" {
		t.Fatalf("target = %q", got)
	}
}
