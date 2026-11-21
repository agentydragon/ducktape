// Package environmentruntimeclient shares the per-RPC transport used to reach a Sandbox's connected Environment Runtime.
package environmentruntimeclient

import (
	"context"
	"errors"
	"strings"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/metadata"
)

// Config describes one call through the Environment Runtime proxy. Every call owns a fresh connection so
// the stable Service can select a ready proxy replica after a rollout.
type Config struct {
	TLS                    *peertls.ClientTLS
	Target                 string
	TokenFile              string
	Timeout                time.Duration
	SandboxName            string
	SandboxUID             string
	SandboxNamespace       string
	ExpectedIncarnationUID string
}

// ExpandTarget fills the Sandbox name and namespace placeholders in an Environment Runtime proxy target.
func ExpandTarget(template, sandbox, namespace string) string {
	return strings.NewReplacer("{sandbox}", sandbox, "{namespace}", namespace).Replace(template)
}

// WithClient supplies call with a per-call bearer.Dial connection, which never retries and reads the
// projected token again for each RPC, under a deadline of config.Timeout. The Sandbox routing metadata
// is appended to the outgoing metadata already on ctx.
func WithClient(ctx context.Context, config Config, call func(context.Context, environmentruntimepb.EnvironmentRuntimeClient) error) error {
	if config.SandboxName == "" || config.SandboxUID == "" || config.SandboxNamespace == "" {
		return errors.New("Environment Runtime proxy Sandbox identity is required")
	}
	if call == nil {
		return errors.New("Environment Runtime proxy call is required")
	}
	conn, err := bearer.Dial(config.Target, config.TokenFile, config.Timeout, config.TLS)
	if err != nil {
		return err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(ctx, config.Timeout)
	defer cancel()
	metadataPairs := []string{
		sandboxapi.RuntimeProxySandboxMetadataKey, config.SandboxName,
		sandboxapi.RuntimeProxySandboxNamespaceMetadataKey, config.SandboxNamespace,
		sandboxapi.RuntimeProxySandboxUIDMetadataKey, config.SandboxUID,
		sandboxapi.RuntimeProxySandboxOwnerNamespaceMetadataKey, config.SandboxNamespace,
		sandboxapi.RuntimeProxySandboxOwnerNameMetadataKey, config.SandboxName,
	}
	if config.ExpectedIncarnationUID != "" {
		metadataPairs = append(metadataPairs, sandboxapi.RuntimeProxyIncarnationUIDMetadataKey, config.ExpectedIncarnationUID)
	}
	ctx = metadata.AppendToOutgoingContext(ctx, metadataPairs...)
	return call(ctx, environmentruntimepb.NewEnvironmentRuntimeClient(conn))
}
