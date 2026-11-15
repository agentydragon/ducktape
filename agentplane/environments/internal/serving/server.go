package serving

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"net/http"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/access"
	"github.com/agentydragon/ducktape/agentplane/environments/internal/resume"
	environmentservice "github.com/agentydragon/ducktape/agentplane/environments/internal/service"
	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	netlistener "github.com/agentydragon/ducktape/agentplane/net/listener"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/keepalive"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type Server struct {
	config       Config
	server       *grpc.Server
	watches      *reader.Watches
	policyHealth *watchhealth.Health
	rpcHealth    *health.Server
	proxy        *runtimeProxy
	mu           sync.RWMutex
	address      string
	serving      bool
}

// New validates local credentials before the manager starts. All API reads used by RPCs bypass
// the manager's leader-scoped caches, including watch streams and authorization conflict retries.
func New(c Config, namespace string, apiConfig *rest.Config, scheme *runtime.Scheme) (*Server, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	credentials, err := c.RPC.ServerCredentialsWithServices(peertls.SandboxControllerURI)
	if err != nil {
		return nil, fmt.Errorf("EnvironmentService listener TLS: %w", err)
	}
	objects, err := client.NewWithWatch(apiConfig, client.Options{Scheme: scheme})
	if err != nil {
		return nil, err
	}
	svids, err := auth.NewKubernetesSVIDResolver(apiConfig, []string{namespace}, &resolver.Identities{PolicyNamespace: c.PolicyNamespace})
	if err != nil {
		return nil, err
	}
	policyHealth := watchhealth.New()
	watches := reader.NewWatches(apiConfig, policyHealth)
	rules := access.NewIndex()
	if err := reader.WatchRules(watches, rules, c.PolicyNamespace, access.Ready); err != nil {
		return nil, err
	}
	manager, err := resume.NewManager(lifecycle.Options{
		Namespace: namespace, Objects: objects, EnvironmentRules: rules,
		PolicyReady: func() error {
			if !policyHealth.Available() {
				return errors.New("environment policy state is unavailable")
			}
			return nil
		},
		WakeServiceCallers:     c.WakeServiceCallers,
		OperatorServiceCallers: c.ServiceOperators,
	})
	if err != nil {
		return nil, err
	}
	service, err := environmentservice.New(namespace, objects, c.RuntimeCatalog, manager, environmentservice.ManagementOptions{
		ServiceReaders: sets.New(c.ServiceReaders...), ServiceManagers: sets.New(c.ServiceManagers...),
		ServiceLaunchers: sets.New(c.ServiceLaunchers...),
	})
	if err != nil {
		return nil, err
	}
	methods, err := environmentservice.WithServiceCallers(environmentservice.Methods(svids), c.ServiceReaders, c.ServiceManagers, c.ServiceLaunchers, c.ServiceOperators)
	if err != nil {
		return nil, err
	}
	identities, err := auth.KubernetesIdentityUIDs(objects, c.PolicyNamespace)
	if err != nil {
		return nil, err
	}
	if d := c.ServiceDelegation; d != nil {
		methods, err = methods.WithServiceDelegation(d.Methods, d.Actors, identities)
		if err != nil {
			return nil, err
		}
	}
	allMethods := auth.HealthMethods()
	maps.Copy(allMethods, methods)
	var proxy *runtimeProxy
	if c.RuntimeProxy != nil {
		proxy, err = newRuntimeProxy(*c.RuntimeProxy, namespace, apiConfig, scheme)
		if err != nil {
			return nil, fmt.Errorf("Runtime proxy: %w", err)
		}
		proxyMethods, err := proxy.gateway.Methods()
		if err != nil {
			return nil, err
		}
		maps.Copy(allMethods, proxyMethods)
	}
	options := append(allMethods.ServerOptions(0), grpc.Creds(credentials), observability.GRPCServerOption(),
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{MinTime: 30 * time.Second}))
	server := grpc.NewServer(options...)
	environmentpb.RegisterEnvironmentServiceServer(server, service)
	if proxy != nil {
		proxy.gateway.RegisterProxy(server)
		proxy.gateway.RegisterExecutor(server)
	}
	rpcHealth := health.NewServer()
	rpcHealth.SetServingStatus("", healthpb.HealthCheckResponse_NOT_SERVING)
	healthpb.RegisterHealthServer(server, rpcHealth)
	return &Server{config: c, server: server, watches: watches, policyHealth: policyHealth, rpcHealth: rpcHealth, proxy: proxy}, nil
}

// NeedLeaderElection keeps RPCs and their independent policy watches running on standbys.
func (*Server) NeedLeaderElection() bool { return false }

func (s *Server) Address() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.address
}

func (s *Server) Check(*http.Request) error {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if !s.serving || !s.policyHealth.Available() {
		return errors.New("EnvironmentService is not ready")
	}
	if s.proxy != nil && !s.proxy.health.Available() {
		return errors.New("Runtime proxy inventory is not ready")
	}
	return nil
}

func (s *Server) Start(ctx context.Context) error {
	listener, err := netlistener.Open(s.config.RPC.ListenAddress)
	if err != nil {
		return fmt.Errorf("EnvironmentService listen: %w", err)
	}
	defer listener.Close()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	watchesDone := make(chan struct{})
	go func() { defer close(watchesDone); s.watches.Run(ctx) }()
	defer func() { <-watchesDone }()
	var proxyDone chan struct{}
	var proxyErr error
	if s.proxy != nil {
		proxyDone = make(chan struct{})
		go func() {
			defer close(proxyDone)
			proxyErr = s.proxy.run(ctx)
		}()
		defer func() { <-proxyDone }()
	}
	served := make(chan error, 1)
	go func() { served <- s.server.Serve(listener) }()
	s.mu.Lock()
	s.address, s.serving = listener.Addr().String(), true
	s.mu.Unlock()
	defer s.server.Stop()
	for {
		changed := s.policyHealth.Changed()
		var proxyChanged <-chan struct{}
		if s.proxy != nil {
			proxyChanged = s.proxy.health.Changed()
		}
		status := healthpb.HealthCheckResponse_NOT_SERVING
		if s.Check(nil) == nil {
			status = healthpb.HealthCheckResponse_SERVING
		}
		s.rpcHealth.SetServingStatus("", status)
		select {
		case <-changed:
			continue
		case <-proxyChanged:
			continue
		case <-proxyDone:
			if ctx.Err() == nil {
				err = fmt.Errorf("Runtime proxy stopped unexpectedly: %w", proxyErr)
			}
		case err = <-served:
			if ctx.Err() == nil {
				err = fmt.Errorf("EnvironmentService listener stopped unexpectedly: %w", err)
			} else {
				err = nil
			}
		case <-ctx.Done():
			err = nil
		}
		break
	}
	s.mu.Lock()
	s.serving = false
	s.mu.Unlock()
	s.policyHealth.MarkDraining()
	s.rpcHealth.Shutdown()
	cancel()
	closing, release := context.WithTimeout(context.Background(), 5*time.Second)
	defer release()
	graceful.Stop(closing, s.server)
	return err
}
