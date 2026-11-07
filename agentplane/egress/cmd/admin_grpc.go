package main

import (
	"context"
	"errors"
	"maps"
	"net"
	"slices"
	"strings"

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
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
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

// adminServer serves Rules, backend administration and probes on one RPC
// listener. The caller policy remains specific to each method.
type adminServer struct {
	server  *grpc.Server
	service *admin.Service
}

func newAdminServer(
	c config,
	reviews auth.TokenReviews,
	options admin.ServiceOptions,
	authentication *identity.Authenticator,
	index *policy.Index,
) (*adminServer, error) {
	rulesMethods := auth.Methods{
		egressrulespb.EgressRules_GetRules_FullMethodName: auth.AnyAccount(authentication),
	}
	methods := auth.HealthMethods()
	maps.Copy(methods, rulesMethods)
	rpc := c.Forward
	// ServerCredentials admits certificate-free TLS for probes and bearer RPCs.
	// Rules and Admin retain their separate per-method authentication policies.
	var serverOptions []grpc.ServerOption
	if rpc.TLS != nil {
		credentials, err := rpc.ServerCredentials(environment_runtime_transport.EgressProxyURI)
		if err != nil {
			return nil, err
		}
		serverOptions = append(serverOptions, grpc.Creds(credentials))
	}
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
		// Administrative tokens remain the only source for these methods.
		// A Sandbox certificate cannot become an operator, even with a bearer.
		if rpc.TLS != nil {
			adminAuth := rpc
			adminAuth.Auth = []listener.Authentication{&listener.KubernetesBearerAuth{Audiences: []string{c.AdminGRPCAudience}}}
			resolver, err = resolver.WithListenerAuthentication(adminAuth, nil)
			if err != nil {
				return nil, err
			}
		}
		maps.Copy(methods, admin.Methods(resolver, callers))
		service = admin.NewService(options)
	}
	serverOptions = append(serverOptions, methods.ServerOptions(0)...)
	serverOptions = append(serverOptions, observability.GRPCServerOption())
	server := grpc.NewServer(serverOptions...)
	healthpb.RegisterHealthServer(server, admin.NewProbes(options.Index))
	egressrulespb.RegisterEgressRulesServer(server, rules.NewService(index))
	if service != nil {
		egressadminpb.RegisterEgressAdminServer(server, service)
	}
	return &adminServer{server: server, service: service}, nil
}

func (s *adminServer) Serve(listener net.Listener) error { return s.server.Serve(listener) }

func (s *adminServer) Shutdown(ctx context.Context) error {
	if s.service != nil {
		s.service.Drain()
	}
	graceful.Stop(ctx, s.server)
	return nil
}
