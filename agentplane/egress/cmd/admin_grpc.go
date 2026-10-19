package main

import (
	"context"
	"errors"
	"net/http"
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

// adminGRPCEnabled is whether the admin listener also serves gRPC.
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

// mountAdminGRPC serves the EgressAdmin API on the listener of the admin HTTP server.
func mountAdminGRPC(adminServer *http.Server, c config, reviews auth.TokenReviews, decisions admin.Decisions, changes admin.Changes) (shutdowner, error) {
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
	grpcServer := grpc.NewServer()
	egressadminpb.RegisterEgressAdminServer(grpcServer, service)
	mountGRPC(adminServer, grpcServer)
	return watchingServer{Server: adminServer, service: service}, nil
}

// mountGRPC serves gRPC on the HTTP server's listener: an HTTP/2 request of content type
// application/grpc goes to grpcServer and every other request to the server's own handler. The
// listener takes cleartext HTTP/2 beside HTTP/1.1 for it.
//
// The gRPC server never serves a listener of its own, so only the HTTP server's Shutdown stops it:
// grpc.Server.GracefulStop cannot drain calls served this way.
func mountGRPC(server *http.Server, grpcServer *grpc.Server) {
	rest := server.Handler
	server.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ProtoMajor == 2 && strings.HasPrefix(r.Header.Get("Content-Type"), "application/grpc") {
			grpcServer.ServeHTTP(w, r)
			return
		}
		rest.ServeHTTP(w, r)
	})
	server.Protocols = new(http.Protocols)
	server.Protocols.SetHTTP1(true)
	server.Protocols.SetUnencryptedHTTP2(true)
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
