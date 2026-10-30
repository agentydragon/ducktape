package main

import (
	"context"
	"errors"
	"net/http"
	"slices"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/admin"
	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
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

// mountAdminGRPC serves the standard health service on the listener of the admin HTTP server, and
// the EgressAdmin API beside it when c configures the admin gRPC API: it admits the callers and the
// audience from c. options holds what the services read.
func mountAdminGRPC(adminServer *http.Server, c config, reviews auth.TokenReviews, options admin.ServiceOptions) (shutdowner, error) {
	grpcServer := grpc.NewServer(observability.GRPCServerOption())
	healthpb.RegisterHealthServer(grpcServer, admin.NewProbes(options.Index))
	var shutdown shutdowner = adminServer
	if c.adminGRPCEnabled() {
		service, err := newAdminService(c, reviews, options)
		if err != nil {
			return nil, err
		}
		egressadminpb.RegisterEgressAdminServer(grpcServer, service)
		shutdown = watchingServer{Server: adminServer, service: service}
	}
	facade.MountGRPC(adminServer, grpcServer)
	return shutdown, nil
}

func newAdminService(c config, reviews auth.TokenReviews, options admin.ServiceOptions) (*admin.Service, error) {
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
	if options.Resolver, err = auth.NewResolver(reviews, c.AdminGRPCAudience, namespaces); err != nil {
		return nil, err
	}
	options.Callers, options.WatchLease = callers, adminWatchLease
	return admin.NewService(options), nil
}

// watchingServer ends the watches its gRPC service holds open before the HTTP server shuts down,
// because Shutdown waits for every call to return.
type watchingServer struct {
	*http.Server
	service *admin.Service
}

func (s watchingServer) Shutdown(ctx context.Context) error {
	s.service.Drain()
	return s.Server.Shutdown(ctx)
}
