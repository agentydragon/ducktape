package runnerclient

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/proto/runner_control"
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
	var clients []runnercontrolpb.RunnerClient
	for _, token := range []string{"first-token", "rotated-token"} {
		if token == "rotated-token" {
			if err := os.WriteFile(tokenFile, []byte(token), 0o600); err != nil {
				t.Fatal(err)
			}
		}
		err := WithClient(base, config, func(ctx context.Context, client runnercontrolpb.RunnerClient) error {
			clients = append(clients, client)
			deadline, ok := ctx.Deadline()
			if !ok || time.Until(deadline) <= 0 {
				t.Error("call context has no live deadline")
			}
			md, ok := metadata.FromOutgoingContext(ctx)
			if !ok || len(md.Get("authorization")) != 1 || md.Get("authorization")[0] != "Bearer "+token || md.Get("trace-id") != nil {
				t.Errorf("outgoing metadata = %v", md)
			}
			if len(md.Get("x-agentplane-sandbox")) != 1 || md.Get("x-agentplane-sandbox")[0] != config.SandboxName ||
				len(md.Get("x-agentplane-sandbox-uid")) != 1 || md.Get("x-agentplane-sandbox-uid")[0] != config.SandboxUID ||
				len(md.Get("x-agentplane-sandbox-owner-namespace")) != 1 || md.Get("x-agentplane-sandbox-owner-namespace")[0] != config.SandboxNamespace ||
				len(md.Get("x-agentplane-sandbox-owner-name")) != 1 || md.Get("x-agentplane-sandbox-owner-name")[0] != config.SandboxName {
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
	if err := WithClient(base, config, func(ctx context.Context, _ runnercontrolpb.RunnerClient) error {
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
