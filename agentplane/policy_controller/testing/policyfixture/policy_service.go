// Package policyfixture serves the production PolicyService for consumer tests.
package policyfixture

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"github.com/agentydragon/ducktape/agentplane/policy_controller/internal/rpc"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// PolicyService serves PolicyService over objects in namespace, over TLS to the exact Actions service SVID as both a
// reader and a writer. No workload-identity issuer is modelled, so DeleteIdentity checks no WorkloadCredentials.
func PolicyService(t *testing.T, namespace string, objects client.WithWatch) policypb.PolicyServiceClient {
	t.Helper()
	service, err := rpc.New(namespace, nil, objects, []string{peertls.ActionsURI}, []string{peertls.ActionsURI})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	stopped := make(chan struct{})
	go func() { _ = service.Start(ctx); close(stopped) }()
	t.Cleanup(func() { cancel(); <-stopped })
	methods, err := service.Methods()
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.ServeTLS(t, peertls.PolicyControllerURI, methods, func(server *grpc.Server) {
		policypb.RegisterPolicyServiceServer(server, service)
	}, peertls.ActionsURI)
	return policypb.NewPolicyServiceClient(grpcmethods.AsCaller(connection, peertls.ActionsURI))
}
