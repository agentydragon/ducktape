package main

import (
	"context"
	"io/fs"
	"net"
	"net/http"

	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/rules"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc"
)

// agentAPI serves the agent API listener: the agent documentation, the OpenAPI document and the
// rules' HTTP/JSON facade. The facade forwards to the gRPC `EgressRules` service on a loopback
// listener of the process's own, so the servicer authenticates every request; nothing outside the
// process calls it.
type agentAPI struct {
	server   *http.Server
	rules    *grpc.Server
	loopback net.Listener
}

// newAgentAPI opens the loopback listener; the facade's connections close when ctx ends.
func newAgentAPI(ctx context.Context, verifier *identity.Verifier, index *policy.Index, agentDocs fs.FS, openAPI []byte) (*agentAPI, error) {
	loopback, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	rulesFacade, err := facade.NewHandler(ctx, loopback.Addr().String(), 0, egressrulespb.RegisterEgressRulesHandlerFromEndpoint)
	if err != nil {
		_ = loopback.Close()
		return nil, err
	}
	rulesGRPC := grpc.NewServer(observability.GRPCServerOption())
	egressrulespb.RegisterEgressRulesServer(rulesGRPC, rules.NewService(verifier, index))
	return &agentAPI{
		server:   &http.Server{Handler: observability.HTTPHandler(rules.New(rulesFacade, agentDocs, openAPI)), ReadHeaderTimeout: readHeaderTimeout},
		rules:    rulesGRPC,
		loopback: loopback,
	}, nil
}

// Serve answers HTTP on listener and the facade's calls on the loopback listener until Shutdown.
func (a *agentAPI) Serve(listener net.Listener) error {
	var group errgroup.Group
	group.Go(func() error { return serving("agent API rules", a.rules.Serve(a.loopback)) })
	group.Go(func() error { return serving("agent API", a.server.Serve(listener)) })
	return group.Wait()
}

// Shutdown lets the admitted requests finish until ctx ends; the facade's calls are gRPC calls,
// so the HTTP server stops first.
func (a *agentAPI) Shutdown(ctx context.Context) error {
	err := a.server.Shutdown(ctx)
	graceful.Stop(ctx, a.rules)
	return err
}
