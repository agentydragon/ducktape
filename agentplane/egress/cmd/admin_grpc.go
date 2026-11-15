package main

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"net"

	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/api/admin/egress_adminpb"
	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/api/rules/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/internal/admin"
	"github.com/agentydragon/ducktape/agentplane/egress/internal/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/internal/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/internal/rules"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

// adminGRPCEnabled is whether the admin listener serves the EgressAdmin API; the standard health service
// is served either way.
func (c *config) adminGRPCEnabled() bool { return len(c.AdminGRPCServiceCallers) > 0 }

func (c *config) validateAdminGRPC() error {
	if err := auth.ValidateServiceCallers(c.AdminGRPCServiceCallers); err != nil {
		return fmt.Errorf("invalid admin_grpc_service_callers: %w", err)
	}
	if c.adminGRPCEnabled() && (c.Forward.TLS == nil || !c.Forward.Has(listener.SPIFFE)) {
		return errors.New("admin_grpc_service_callers requires forward TLS and SPIFFE client trust")
	}
	return nil
}

// adminServer serves Rules, backend administration and probes on one RPC
// listener. The caller policy remains specific to each method.
type adminServer struct {
	server  *grpc.Server
	service *admin.Service
}

func newAdminServer(
	c config,
	options admin.ServiceOptions,
	authentication *identity.Authenticator,
	index *policy.Index,
) (*adminServer, error) {
	if err := c.validateAdminGRPC(); err != nil {
		return nil, err
	}
	rulesMethods := auth.Methods{
		egressrulespb.EgressRules_GetRules_FullMethodName: auth.AnySubject(authentication),
	}
	methods := auth.HealthMethods()
	maps.Copy(methods, rulesMethods)
	rpc := c.Forward
	// ServerCredentials admits certificate-free TLS for probes.
	// Rules and Admin retain their separate per-method authentication policies.
	var serverOptions []grpc.ServerOption
	if rpc.TLS != nil {
		var transportCredentials credentials.TransportCredentials
		var err error
		if len(c.AdminGRPCServiceCallers) > 0 {
			transportCredentials, err = rpc.ServerCredentialsWithServices(peertls.EgressProxyURI)
		} else {
			transportCredentials, err = rpc.ServerCredentials(peertls.EgressProxyURI)
		}
		if err != nil {
			return nil, err
		}
		serverOptions = append(serverOptions, grpc.Creds(transportCredentials))
	}
	var service *admin.Service
	if c.adminGRPCEnabled() {
		adminMethods, err := admin.Methods(c.AdminGRPCServiceCallers)
		if err != nil {
			return nil, err
		}
		maps.Copy(methods, adminMethods)
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
