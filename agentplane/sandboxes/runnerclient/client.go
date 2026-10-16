// Package runnerclient shares the per-RPC transport used to reach a Sandbox's connected runner.
package runnerclient

import (
	"context"
	"errors"
	"os"
	"strings"
	"time"

	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/proto/runner_control"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
)

// Config describes one call through the Runner Gateway proxy. Every call reads the projected token
// again and owns a fresh connection so DNS can resolve a new connection holder after a handoff.
type Config struct {
	Target                  string
	TokenFile               string
	Timeout                 time.Duration
	SandboxName             string
	SandboxUID              string
	SandboxNamespace        string
	DisableRetry            bool
	ReplaceOutgoingMetadata bool
}

// ExpandTarget fills the Sandbox name and namespace placeholders in a control Service target.
func ExpandTarget(template, sandbox, namespace string) string {
	return strings.NewReplacer("{sandbox}", sandbox, "{namespace}", namespace).Replace(template)
}

// WithClient reads the current token, creates a per-call gRPC connection and deadline, and supplies
// the authenticated connection to call. ReplaceOutgoingMetadata preserves the dispatcher's existing
// behavior; callers that append metadata leave it false.
func WithClient(ctx context.Context, config Config, call func(context.Context, runnercontrolpb.RunnerClient) error) error {
	if config.Timeout <= 0 {
		return errors.New("runner request timeout must be positive")
	}
	if config.Target == "" || config.TokenFile == "" || config.SandboxName == "" ||
		config.SandboxUID == "" || config.SandboxNamespace == "" {
		return errors.New("runner proxy target, token file, and Sandbox identity are required")
	}
	if call == nil {
		return errors.New("runner proxy call is required")
	}
	data, err := os.ReadFile(config.TokenFile)
	if err != nil {
		return err
	}
	token := strings.TrimSpace(string(data))
	if token == "" {
		return errors.New("runner proxy token is empty")
	}
	dialOptions := []grpc.DialOption{grpc.WithTransportCredentials(insecure.NewCredentials())}
	if config.DisableRetry {
		dialOptions = append(dialOptions, grpc.WithDisableRetry())
	}
	conn, err := grpc.NewClient(config.Target, dialOptions...)
	if err != nil {
		return err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(ctx, config.Timeout)
	defer cancel()
	metadataPairs := []string{
		"authorization", "Bearer " + token,
		"x-agentplane-sandbox", config.SandboxName,
		"x-agentplane-sandbox-uid", config.SandboxUID,
		"x-agentplane-sandbox-owner-namespace", config.SandboxNamespace,
		"x-agentplane-sandbox-owner-name", config.SandboxName,
	}
	if config.ReplaceOutgoingMetadata {
		ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs(metadataPairs...))
	} else {
		ctx = metadata.AppendToOutgoingContext(ctx, metadataPairs...)
	}
	return call(ctx, runnercontrolpb.NewRunnerClient(conn))
}
