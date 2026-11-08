package main

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"maps"
	"net"
	"slices"
	"strings"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/egress/admin"
	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/rules"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/soheilhy/cmux"
	"google.golang.org/grpc"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

// adminGRPCEnabled is whether the admin listener serves the EgressAdmin API; the standard health service
// is served either way.
func (c *config) adminGRPCEnabled() bool { return c.AdminGRPCAudience != "" }

// validateAdminGRPC refuses a gRPC API half configured: an audience with nobody admitted, or callers
// who could not prove themselves, would silently serve nothing.
func (c *config) validateAdminGRPC() error {
	if c.adminGRPCEnabled() != (len(c.AdminGRPCCallers) != 0) {
		return errors.New("admin_grpc_audience and admin_grpc_callers are set together or not at all")
	}
	_, err := c.adminGRPCCallers()
	return err
}

// adminGRPCCallers parses admin_grpc_callers, each entry a `namespace/name`.
func (c *config) adminGRPCCallers() ([]auth.Account, error) {
	callers := make([]auth.Account, len(c.AdminGRPCCallers))
	for i, entry := range c.AdminGRPCCallers {
		namespace, name, found := strings.Cut(entry, "/")
		if !found || namespace == "" || name == "" || strings.Contains(name, "/") {
			return nil, errors.New("admin_grpc_callers entries must be namespace/name")
		}
		callers[i] = auth.Account{Namespace: namespace, Name: name}
	}
	return callers, nil
}

// adminServer serves rules over the forward listener's TLS identity when TLS
// is configured. Its cleartext endpoint remains the independent probe/admin
// endpoint and also carries Rules so the shared authenticator can refuse it.
type adminServer struct {
	plain   *grpc.Server
	secured *grpc.Server
	service *admin.Service

	mu      sync.Mutex
	mux     cmux.CMux
	stopped bool
}

// newAdminServer applies the same workload authenticator to Rules on both
// transport variants. The secured gRPC server receives the exact TLS config
// used by the forward listener; only the plaintext server serves probes and
// the separately authorized EgressAdmin API.
func newAdminServer(
	c config,
	reviews auth.TokenReviews,
	options admin.ServiceOptions,
	authentication *identity.Authenticator,
	index *policy.Index,
	serverTLS *tls.Config,
) (*adminServer, error) {
	rulesMethods := auth.Methods{
		egressrulespb.EgressRules_GetRules_FullMethodName: auth.AnyAccount(authentication),
	}
	plainMethods := auth.HealthMethods()
	maps.Copy(plainMethods, rulesMethods)
	var service *admin.Service
	if c.adminGRPCEnabled() {
		callers, err := c.adminGRPCCallers()
		if err != nil {
			return nil, err
		}
		namespaces := make([]string, 0, len(callers))
		for _, caller := range callers {
			if !slices.Contains(namespaces, caller.Namespace) {
				namespaces = append(namespaces, caller.Namespace)
			}
		}
		resolver, err := auth.NewResolver(reviews, c.AdminGRPCAudience, namespaces)
		if err != nil {
			return nil, err
		}
		maps.Copy(plainMethods, admin.Methods(resolver, callers))
		service = admin.NewService(options)
	}
	plain := grpc.NewServer(append(plainMethods.ServerOptions(0), observability.GRPCServerOption())...)
	rulesService := rules.NewService(index)
	healthpb.RegisterHealthServer(plain, admin.NewProbes(options.Index))
	egressrulespb.RegisterEgressRulesServer(plain, rulesService)
	if service != nil {
		egressadminpb.RegisterEgressAdminServer(plain, service)
	}
	secured := (*grpc.Server)(nil)
	if serverTLS != nil {
		credentials, err := environment_runtime_transport.ServerCredentials(serverTLS)
		if err != nil {
			return nil, err
		}
		securedOptions := append(rulesMethods.ServerOptions(0), grpc.Creds(credentials), observability.GRPCServerOption())
		secured = grpc.NewServer(securedOptions...)
		egressrulespb.RegisterEgressRulesServer(secured, rulesService)
	}
	return &adminServer{plain: plain, secured: secured, service: service}, nil
}

// Serve shares the admin port by transport: TLS connections are served with
// the forward listener's native gRPC credentials; cleartext HTTP/2 remains for
// probes and the independently authorized admin API.
func (s *adminServer) Serve(listener net.Listener) error {
	if s.secured == nil {
		s.mu.Lock()
		stopped := s.stopped
		s.mu.Unlock()
		if stopped {
			_ = listener.Close()
			return nil
		}
		return s.plain.Serve(listener)
	}
	mux := cmux.New(listener)
	tlsListener := mux.Match(cmux.TLS())
	plainListener := mux.Match(cmux.HTTP2())
	s.mu.Lock()
	if s.stopped {
		s.mu.Unlock()
		_ = listener.Close()
		return nil
	}
	s.mux = mux
	s.mu.Unlock()

	failed := make(chan error, 3)
	go func() {
		failed <- adminServeError("plaintext admin gRPC", func() error { return s.plain.Serve(plainListener) })
	}()
	go func() {
		failed <- adminServeError("TLS Rules gRPC", func() error { return s.secured.Serve(tlsListener) })
	}()
	go func() { failed <- adminServeError("admin listener multiplexer", mux.Serve) }()
	err := <-failed
	s.mu.Lock()
	stopped := s.stopped
	s.mu.Unlock()
	if !stopped {
		// Stop new accepts and let gRPC drain existing calls. Main observes this
		// failure and owns the bounded drain/force-stop sequence.
		s.closeMux()
		_ = listener.Close()
	}
	<-failed
	<-failed
	s.mu.Lock()
	stopped = s.stopped
	s.mu.Unlock()
	if stopped {
		return nil
	}
	return err
}

// Shutdown ends the open watches once, then drains both transport servers and
// closes their shared listener.
func (s *adminServer) Shutdown(ctx context.Context) error {
	s.mu.Lock()
	s.stopped = true
	s.mu.Unlock()
	if s.service != nil {
		s.service.Drain()
	}
	graceful.Stop(ctx, s.plain)
	if s.secured != nil {
		graceful.Stop(ctx, s.secured)
	}
	s.closeMux()
	return nil
}

func adminServeError(name string, serve func() error) error {
	err := serve()
	if err == nil {
		return fmt.Errorf("%s stopped without an error", name)
	}
	return fmt.Errorf("%s: %w", name, err)
}

func (s *adminServer) closeMux() {
	s.mu.Lock()
	mux := s.mux
	s.mux = nil
	s.mu.Unlock()
	if mux != nil {
		mux.Close()
	}
}
