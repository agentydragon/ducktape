package main

import (
	"context"
	"crypto/tls"
	"io/fs"
	"net/http"

	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/rules"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadfacade "github.com/agentydragon/ducktape/agentplane/workload_auth/facade"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// newRulesWorkloadAPI accepts only a TLS peer whose fixed SVID resolves to the currently bound
// Sandbox. The direct handler has no bearer resolver and cannot fall back to a token.
func newRulesWorkloadAPI(
	ctx context.Context,
	objects client.Reader,
	allowedNamespaces []string,
	index *policy.Index,
	tlsConfig *tls.Config,
	agentDocs fs.FS,
	openAPI []byte,
) (*http.Server, error) {
	svid, err := auth.NewSVIDResolver(objects, allowedNamespaces)
	if err != nil {
		return nil, err
	}
	methods := auth.Methods{
		egressrulespb.EgressRules_GetRules_FullMethodName: auth.SVIDAccount(svid),
	}
	service := rules.NewService(index)
	registered := workloadfacade.Registration{
		Server: func(registrar grpc.ServiceRegistrar) {
			egressrulespb.RegisterEgressRulesServer(registrar, service)
		},
		HTTP: func(ctx context.Context, mux *runtime.ServeMux, channel grpc.ClientConnInterface) error {
			return egressrulespb.RegisterEgressRulesHandlerClient(ctx, mux, egressrulespb.NewEgressRulesClient(channel))
		},
	}
	api, err := workloadfacade.NewHandler(ctx,
		workloadfacade.Interceptors{
			Unary: methods.UnaryInterceptor(0), Stream: methods.StreamInterceptor(0),
		}, facade.HandlerOptions{RejectUnknownJSONFields: true}, registered)
	if err != nil {
		return nil, err
	}
	return &http.Server{
		Handler: observability.HTTPHandler(rules.New(api, agentDocs, openAPI)), TLSConfig: tlsConfig, ReadHeaderTimeout: readHeaderTimeout,
		ConnState: environment_runtime_transport.HTTPServerTLSExpiryConnState(nil),
	}, nil
}
