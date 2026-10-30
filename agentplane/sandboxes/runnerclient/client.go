// Package runnerclient shares the per-RPC transport used to reach a Sandbox's connected runner.
package runnerclient

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/metadata"
)

// Config describes one call through the Runner Gateway proxy. Every call owns a fresh connection so
// DNS can resolve a new connection holder after a handoff.
type Config struct {
	Target                  string
	TokenFile               string
	Timeout                 time.Duration
	SandboxName             string
	SandboxUID              string
	SandboxNamespace        string
	ReplaceOutgoingMetadata bool
}

// ExpandTarget fills the Sandbox name and namespace placeholders in a control Service target.
func ExpandTarget(template, sandbox, namespace string) string {
	return strings.NewReplacer("{sandbox}", sandbox, "{namespace}", namespace).Replace(template)
}

// WithClient supplies call with a per-call bearer.Dial connection, which never retries and reads the
// projected token again for each RPC, under a deadline of config.Timeout. ReplaceOutgoingMetadata
// preserves the dispatcher's existing behavior; callers that append metadata leave it false.
func WithClient(ctx context.Context, config Config, call func(context.Context, runnerservicepb.RunnerClient) error) error {
	if config.SandboxName == "" || config.SandboxUID == "" || config.SandboxNamespace == "" {
		return errors.New("runner proxy Sandbox identity is required")
	}
	if call == nil {
		return errors.New("runner proxy call is required")
	}
	conn, err := bearer.Dial(config.Target, config.TokenFile, config.Timeout)
	if err != nil {
		return err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(ctx, config.Timeout)
	defer cancel()
	metadataPairs := []string{
		sandboxapi.RunnerProxySandboxMetadataKey, config.SandboxName,
		sandboxapi.RunnerProxySandboxUIDMetadataKey, config.SandboxUID,
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey, config.SandboxNamespace,
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey, config.SandboxName,
	}
	if config.ReplaceOutgoingMetadata {
		ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs(metadataPairs...))
	} else {
		ctx = metadata.AppendToOutgoingContext(ctx, metadataPairs...)
	}
	return call(ctx, runnerservicepb.NewRunnerClient(conn))
}
