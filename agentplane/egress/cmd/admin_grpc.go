package main

import (
	"context"
	"errors"
	"slices"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/admin"
	egressadminpb "github.com/agentydragon/ducktape/agentplane/proto/egress_admin"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
)

// adminWatchLease is how long a decision watch runs before the caller subscribes again. The bearer is
// reviewed when a watch opens, so this bounds how long a revoked token keeps reading.
const adminWatchLease = 10 * time.Minute

// validateAdminGRPC refuses a listener half configured: a port with nobody admitted to it, or an
// audience and callers that would silently serve nothing.
func (c *config) validateAdminGRPC() error {
	if c.AdminGRPCPort == nil {
		if c.AdminGRPCAudience != "" || len(c.AdminGRPCCallers) != 0 {
			return errors.New("admin_grpc_audience and admin_grpc_callers are set only with admin_grpc_port")
		}
		return nil
	}
	if c.AdminGRPCAudience == "" || len(c.AdminGRPCCallers) == 0 {
		return errors.New("admin_grpc_port needs admin_grpc_audience and admin_grpc_callers")
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

// adminGRPC is the admin API's gRPC listener, served beside the HTTP admin listener.
type adminGRPC struct {
	service *admin.Service
	server  *grpc.Server
}

func newAdminGRPC(c config, reviews auth.TokenReviews, decisions admin.Decisions, changes admin.Changes) (*adminGRPC, error) {
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
	service := admin.NewService(decisions, changes, resolver, callers, adminWatchLease)
	server := grpc.NewServer()
	egressadminpb.RegisterEgressAdminServer(server, service)
	return &adminGRPC{service: service, server: server}, nil
}

// Shutdown ends the open watches, lets other calls finish, and stops the server when ctx ends.
func (a *adminGRPC) Shutdown(ctx context.Context) error {
	a.service.Drain()
	stopped := make(chan struct{})
	go func() { a.server.GracefulStop(); close(stopped) }()
	select {
	case <-stopped:
		return nil
	case <-ctx.Done():
		a.server.Stop()
		return ctx.Err()
	}
}
