package gateway

import (
	"context"
	"fmt"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/executor/executorpb"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
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
// runner connections use explicitly configured transport security and incarnation-fenced metadata.
type Config struct {
	RunnerSecurity           runner_transport.Security
	Namespace                string
	CallerAccounts           []auth.Account
	SandboxControllerAccount *auth.Account
	HolderName               string
	HolderIP                 string
	ProxyPort                int32
	ControlTiming            ControlTiming
	// ExecutorCallers enables the typed execution API independently from the Runner proxy's
	// caller allowlist. An empty set leaves the public service unregistered.
	ExecutorCallers []auth.Account
}

// ControlTiming bounds stream liveness and Lease renewal work.
type ControlTiming struct {
	HeartbeatInterval  time.Duration
	LivenessTimeout    time.Duration
	LeaseTouchInterval time.Duration
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
	return timing
}

func validateControlTiming(timing ControlTiming) error {
	if timing.HeartbeatInterval <= 0 || timing.LivenessTimeout <= 0 ||
		timing.LeaseTouchInterval <= 0 {
		return fmt.Errorf("Gateway control timings must be positive")
	}
	if timing.HeartbeatInterval >= timing.LivenessTimeout ||
		timing.LeaseTouchInterval >= timing.LivenessTimeout ||
		timing.LivenessTimeout > time.Duration(leaseDurationSeconds)*time.Second {
		return fmt.Errorf("Gateway control timing exceeds the runner liveness or Lease bound")
	}
	return nil
}

// Gateway serves the per-Sandbox Runner proxy and dials each Runner's authenticated gRPC server.
type Gateway struct {
	config      Config
	callerAuth  *auth.Resolver
	sandboxes   *sandboxResolver
	connections *connections
	records     *connectionRecords
	objects     client.Reader
	// runnerWake makes RunRunnerObservers rescan now rather than at its next tick; see RunnerDiscoveryHandler.
	runnerWake chan struct{}
}

func New(
	config Config,
	callerAuth *auth.Resolver,
	objects client.Reader,
	core coreclient.CoreV1Interface,
	coordination coordinationclient.CoordinationV1Interface,
	discovery discoveryclient.DiscoveryV1Interface,
) (*Gateway, error) {
	if err := config.RunnerSecurity.Validate(); err != nil {
		return nil, fmt.Errorf("runner security: %w", err)
	}
	if config.Namespace == "" || len(config.CallerAccounts) == 0 || callerAuth == nil {
		return nil, fmt.Errorf("Gateway namespace, caller policy and token resolvers are required")
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
	for _, account := range config.CallerAccounts {
		if account.Namespace == "" || account.Name == "" {
			return nil, fmt.Errorf("Gateway caller account namespace and name are required")
		}
	}
	for _, account := range config.ExecutorCallers {
		if account.Namespace == "" || account.Name == "" {
			return nil, fmt.Errorf("Gateway executor caller account namespace and name are required")
		}
	}
	if config.SandboxControllerAccount != nil {
		if config.SandboxControllerAccount.Namespace == "" || config.SandboxControllerAccount.Name == "" {
			return nil, fmt.Errorf("Sandbox controller account namespace and name are required")
		}
		copy := *config.SandboxControllerAccount
		config.SandboxControllerAccount = &copy
	}
	return &Gateway{
		config: config, callerAuth: callerAuth, objects: objects,
		sandboxes: sandboxes, connections: newConnections(), records: records,
		runnerWake: make(chan struct{}, 1),
	}, nil
}

// Methods admits the callers of the Runner proxy and the typed execution API. Only the Sandbox controller may
// quiesce a runner; with no controller configured, nobody may.
func (g *Gateway) Methods() auth.Methods {
	callers := auth.Accounts(g.callerAuth, g.config.CallerAccounts)
	var controllers []auth.Account
	if g.config.SandboxControllerAccount != nil {
		controllers = []auth.Account{*g.config.SandboxControllerAccount}
	}
	executors := auth.Accounts(g.callerAuth, g.config.ExecutorCallers)
	return auth.Methods{
		runnerservicepb.Runner_ListSessions_FullMethodName:          callers,
		runnerservicepb.Runner_GetStatus_FullMethodName:             callers,
		runnerservicepb.Runner_OpenSession_FullMethodName:           callers,
		runnerservicepb.Runner_SubmitInput_FullMethodName:           callers,
		runnerservicepb.Runner_ChangeModel_FullMethodName:           callers,
		runnerservicepb.Runner_ChangeReasoningEffort_FullMethodName: callers,
		runnerservicepb.Runner_InterruptTurn_FullMethodName:         callers,
		runnerservicepb.Runner_Quiesce_FullMethodName:               auth.Accounts(g.callerAuth, controllers),
		executorpb.Executor_OpenSession_FullMethodName:              executors,
		executorpb.Executor_StartProcess_FullMethodName:             executors,
		executorpb.Executor_ReadProcess_FullMethodName:              executors,
		executorpb.Executor_WriteProcess_FullMethodName:             executors,
		executorpb.Executor_SignalProcess_FullMethodName:            executors,
		executorpb.Executor_TerminateProcess_FullMethodName:         executors,
		executorpb.Executor_GetFileMetadata_FullMethodName:          executors,
		executorpb.Executor_OpenFile_FullMethodName:                 executors,
		executorpb.Executor_ReadFile_FullMethodName:                 executors,
		executorpb.Executor_CloseFile_FullMethodName:                executors,
	}
}

// RegisterProxy installs the per-Sandbox Runner proxy.
func (g *Gateway) RegisterProxy(server *grpc.Server) {
	runnerservicepb.RegisterRunnerServer(server, &runnerProxy{gateway: g})
}

// RegisterExecutor enables the typed execution path only when its own caller allowlist is
// configured. Existing Gateway deployments therefore keep the RPC absent by default.
func (g *Gateway) RegisterExecutor(server *grpc.Server) {
	if len(g.config.ExecutorCallers) == 0 {
		return
	}
	executorpb.RegisterExecutorServer(server, &executorServer{gateway: g})
}

// WatchConnectionLeases must sync before the public gRPC services start accepting requests.
func (g *Gateway) WatchConnectionLeases(ctx context.Context, client kubernetes.Interface) error {
	return watchConnectionLeases(ctx, client, g.config.Namespace, g.connections)
}
