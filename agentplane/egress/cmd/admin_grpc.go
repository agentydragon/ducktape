package main

import (
	"context"
	"errors"
	"maps"
	"slices"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/admin"
	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

// adminWatchLease is how long a decision watch runs before the caller subscribes again. The bearer is
// reviewed when a watch opens, so this bounds how long a revoked token keeps reading.
const adminWatchLease = 10 * time.Minute

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

// adminServer is the admin listener's gRPC server: the standard health service, and the EgressAdmin API
// beside it when service is set.
type adminServer struct {
	*grpc.Server
	service *admin.Service
}

// newAdminServer serves the EgressAdmin API when c configures the admin gRPC API, admitting the
// callers and the audience from c. options holds what the services read.
func newAdminServer(c config, reviews auth.TokenReviews, options admin.ServiceOptions) (adminServer, error) {
	methods := auth.HealthMethods()
	var service *admin.Service
	if c.adminGRPCEnabled() {
		callers, err := c.adminGRPCCallers()
		if err != nil {
			return adminServer{}, err
		}
		namespaces := make([]string, 0, len(callers))
		for _, caller := range callers {
			if !slices.Contains(namespaces, caller.Namespace) {
				namespaces = append(namespaces, caller.Namespace)
			}
		}
		resolver, err := auth.NewResolver(reviews, c.AdminGRPCAudience, namespaces)
		if err != nil {
			return adminServer{}, err
		}
		maps.Copy(methods, admin.Methods(resolver, callers))
		options.WatchLease = adminWatchLease
		service = admin.NewService(options)
	}
	grpcServer := grpc.NewServer(append(methods.ServerOptions(0), observability.GRPCServerOption())...)
	healthpb.RegisterHealthServer(grpcServer, admin.NewProbes(options.Index))
	if service != nil {
		egressadminpb.RegisterEgressAdminServer(grpcServer, service)
	}
	return adminServer{Server: grpcServer, service: service}, nil
}

// Shutdown ends the open watches, which would otherwise hold the drain until ctx ends, and stops
// the server once the other calls have finished or ctx has ended.
func (s adminServer) Shutdown(ctx context.Context) error {
	if s.service != nil {
		s.service.Drain()
	}
	graceful.Stop(ctx, s.Server)
	return nil
}
