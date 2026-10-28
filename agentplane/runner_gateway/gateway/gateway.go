package gateway

import (
	"context"
	"fmt"
	"time"

	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	executor_gatewaypb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"k8s.io/client-go/kubernetes"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	coreclient "k8s.io/client-go/kubernetes/typed/core/v1"
	discoveryclient "k8s.io/client-go/kubernetes/typed/discovery/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Config contains the per-process Gateway role wiring. Public callers use TokenReview;
// runner transports use certificate identities on separate private connections.
type Config struct {
	RunnerTransports         []string
	RunnerTLS                runner_transport.Config
	Namespace                string
	CallerAccounts           []auth.Account
	SandboxControllerAccount *auth.Account
	HolderName               string
	HolderIP                 string
	ProxyPort                int32
	ControlTiming            ControlTiming
	// ExecutorCallers enables the opt-in raw executor WebSocket tunnel independently from the
	// Runner proxy's caller allowlist. An empty set leaves the public RPC unregistered.
	ExecutorCallers []auth.Account
}

// ControlTiming bounds stream liveness and Lease renewal work.
type ControlTiming struct {
	HeartbeatInterval  time.Duration
	LivenessTimeout    time.Duration
	LeaseTouchInterval time.Duration
	MaximumAge         time.Duration
}

func defaultControlTiming(timing ControlTiming) ControlTiming {
	if timing.HeartbeatInterval == 0 {
		timing.HeartbeatInterval = 10 * time.Second
	}
	if timing.LivenessTimeout == 0 {
		timing.LivenessTimeout = 30 * time.Second
	}
	if timing.LeaseTouchInterval == 0 {
		timing.LeaseTouchInterval = 15 * time.Second
	}
	if timing.MaximumAge == 0 {
		timing.MaximumAge = time.Hour
	}
	return timing
}

func validateControlTiming(timing ControlTiming) error {
	if timing.HeartbeatInterval <= 0 || timing.LivenessTimeout <= 0 ||
		timing.LeaseTouchInterval <= 0 || timing.MaximumAge <= 0 {
		return fmt.Errorf("Gateway control timings must be positive")
	}
	if timing.HeartbeatInterval >= timing.LivenessTimeout ||
		timing.LeaseTouchInterval >= timing.LivenessTimeout ||
		timing.LivenessTimeout > time.Duration(leaseDurationSeconds)*time.Second {
		return fmt.Errorf("Gateway control timing exceeds the runner liveness or Lease bound")
	}
	return nil
}

// Gateway serves the per-Sandbox Runner proxy and accepts runner-initiated connections on the
// separately registered control listener.
type Gateway struct {
	config          Config
	callerAuth      *auth.Resolver
	callers         map[auth.Account]struct{}
	executorCallers map[auth.Account]struct{}
	sandboxes       *sandboxResolver
	connections     *connections
	records         *connectionRecords
	controller      *auth.Account
	objects         client.Reader
}

func New(
	config Config,
	callerAuth *auth.Resolver,
	objects client.Reader,
	core coreclient.CoreV1Interface,
	coordination coordinationclient.CoordinationV1Interface,
	discovery discoveryclient.DiscoveryV1Interface,
) (*Gateway, error) {
	if config.Namespace == "" || len(config.CallerAccounts) == 0 || callerAuth == nil {
		return nil, fmt.Errorf("Gateway namespace, caller policy and token resolvers are required")
	}
	if len(config.RunnerTransports) == 0 {
		return nil, fmt.Errorf("runner transports must explicitly select forward, reverse, or both")
	}
	seen := make(map[string]bool)
	for _, mode := range config.RunnerTransports {
		if (mode != "forward" && mode != "reverse") || seen[mode] {
			return nil, fmt.Errorf("invalid or duplicate runner transport %q", mode)
		}
		seen[mode] = true
	}
	if config.HolderName == "" || config.HolderIP == "" || config.ProxyPort < 1 {
		return nil, fmt.Errorf("Gateway holder Pod name, IP and proxy port are required")
	}
	config.ControlTiming = defaultControlTiming(config.ControlTiming)
	if err := validateControlTiming(config.ControlTiming); err != nil {
		return nil, err
	}
	sandboxes, err := newSandboxResolver(config.Namespace, objects, core)
	if err != nil {
		return nil, err
	}
	records, err := newConnectionRecords(coordination, discovery, config.Namespace, connectionHolder{
		name: config.HolderName, ip: config.HolderIP, port: config.ProxyPort,
	})
	if err != nil {
		return nil, err
	}
	callers := make(map[auth.Account]struct{}, len(config.CallerAccounts))
	for _, account := range config.CallerAccounts {
		if account.Namespace == "" || account.Name == "" {
			return nil, fmt.Errorf("Gateway caller account namespace and name are required")
		}
		callers[account] = struct{}{}
	}
	executorCallers := make(map[auth.Account]struct{}, len(config.ExecutorCallers))
	for _, account := range config.ExecutorCallers {
		if account.Namespace == "" || account.Name == "" {
			return nil, fmt.Errorf("Gateway executor caller account namespace and name are required")
		}
		executorCallers[account] = struct{}{}
	}
	var controller *auth.Account
	if config.SandboxControllerAccount != nil {
		if config.SandboxControllerAccount.Namespace == "" || config.SandboxControllerAccount.Name == "" {
			return nil, fmt.Errorf("Sandbox controller account namespace and name are required")
		}
		copy := *config.SandboxControllerAccount
		controller = &copy
	}
	return &Gateway{
		config: config, callerAuth: callerAuth, callers: callers, executorCallers: executorCallers, objects: objects,
		sandboxes: sandboxes, connections: newConnections(), records: records,
		controller: controller,
	}, nil
}

// RegisterProxy installs the per-Sandbox Runner proxy. RunnerControl.Connect is deliberately
// registered on a separate listener.
func (g *Gateway) RegisterProxy(server *grpc.Server) {
	runnerservicepb.RegisterRunnerServer(server, &runnerProxy{gateway: g})
}

// RegisterExecutorGateway enables the central executor path only when its own caller allowlist is
// configured. Existing Gateway deployments therefore keep the RPC absent by default.
func (g *Gateway) RegisterExecutorGateway(server *grpc.Server) {
	if len(g.executorCallers) == 0 {
		return
	}
	executor_gatewaypb.RegisterExecutorGatewayServer(server, &executorGatewayServer{gateway: g})
}

// RegisterRunnerControl serves the runner-initiated bidirectional connection on the private
// mTLS listener when reverse transport is enabled.
func (g *Gateway) RegisterRunnerControl(server *grpc.Server) {
	for _, mode := range g.config.RunnerTransports {
		if mode == "reverse" {
			runnercontrolpb.RegisterRunnerControlServer(server, &runnerControlServer{gateway: g})
		}
	}
}

// WatchConnectionLeases must sync before the public gRPC services start accepting requests.
func (g *Gateway) WatchConnectionLeases(ctx context.Context, client kubernetes.Interface) error {
	return watchConnectionLeases(ctx, client, g.config.Namespace, g.connections)
}

func (g *Gateway) authorize(ctx context.Context, allowed map[auth.Account]struct{}) error {
	_, err := g.callerAuth.RequireAccount(ctx, allowed)
	return err
}
