package main

import (
	"context"
	"io/fs"
	"net/http"

	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/rules"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"google.golang.org/grpc"
)

// newAgentAPI is the server of the agent API listener at address: the agent documentation, the
// OpenAPI document and the rules' HTTP/JSON facade beside the gRPC `EgressRules` service. The facade
// forwards to that service over address itself, so the servicer authenticates every request; it runs
// until ctx ends.
func newAgentAPI(ctx context.Context, address string, verifier *identity.Verifier, index *policy.Index, agentDocs fs.FS, openAPI []byte) (*http.Server, error) {
	rulesFacade, err := facade.NewHandler(ctx, address, 0, egressrulespb.RegisterEgressRulesHandlerFromEndpoint)
	if err != nil {
		return nil, err
	}
	server := &http.Server{Handler: observability.HTTPHandler(rules.New(rulesFacade, agentDocs, openAPI)), ReadHeaderTimeout: readHeaderTimeout}
	rulesGRPC := grpc.NewServer(observability.GRPCServerOption())
	egressrulespb.RegisterEgressRulesServer(rulesGRPC, rules.NewService(verifier, index))
	facade.MountGRPC(server, rulesGRPC)
	return server, nil
}
