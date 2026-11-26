package identitycache

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/observability"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"google.golang.org/grpc"
)

// StandardGrace is how long services keep answering from the view after its watch breaks: long enough to ride out a
// Policy Controller replica restart, and the bound on how long a deleted Identity can still authorize while the watch
// is down.
const StandardGrace = 30 * time.Second

// Peer is a service's `policy_service` settings block: PolicyService reached over mutual TLS with the service's SVID.
type Peer struct {
	Target string            `mapstructure:"target" json:"target" yaml:"target"`
	TLS    peertls.ClientTLS `mapstructure:"tls" json:"tls" yaml:"tls"`
}

// Validate requires a target and a mutual-TLS caller presenting clientURI to the Policy Controller.
func (p Peer) Validate(clientURI string) error {
	if strings.TrimSpace(p.Target) == "" {
		return errors.New("policy_service.target is required")
	}
	if err := p.TLS.ValidateCaller("", clientURI); err != nil {
		return fmt.Errorf("policy_service.tls: %w", err)
	}
	if p.TLS.ServerURI != peertls.PolicyControllerURI {
		return fmt.Errorf("policy_service.tls.server_uri must be %q", peertls.PolicyControllerURI)
	}
	return nil
}

// Start dials PolicyService and runs a Cache until ctx ends. stop ends it, waits for it and closes the connection.
func Start(ctx context.Context, peer Peer, grace time.Duration) (*Cache, func(), error) {
	credentials, err := peer.TLS.Credentials()
	if err != nil {
		return nil, nil, err
	}
	connection, err := grpc.NewClient(peer.Target, observability.GRPCClientOption(), grpc.WithTransportCredentials(credentials))
	if err != nil {
		return nil, nil, err
	}
	cache, err := New(policypb.NewPolicyServiceClient(connection), grace)
	if err != nil {
		_ = connection.Close()
		return nil, nil, err
	}
	runContext, stopRunning := context.WithCancel(ctx)
	running := make(chan struct{})
	go func() { defer close(running); _ = cache.Run(runContext) }()
	return cache, func() { stopRunning(); <-running; _ = connection.Close() }, nil
}
