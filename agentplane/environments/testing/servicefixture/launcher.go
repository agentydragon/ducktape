// Package servicefixture composes production EnvironmentService handlers for consumer tests.
package servicefixture

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environments/internal/resume"
	environmentservice "github.com/agentydragon/ducktape/agentplane/environments/internal/service"
	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"k8s.io/apimachinery/pkg/util/sets"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Launcher serves production launch handlers over TLS to the exact Actions service SVID.
func Launcher(t *testing.T, namespace string, objects client.WithWatch) environmentpb.EnvironmentServiceClient {
	t.Helper()
	identities, err := auth.KubernetesIdentityUIDs(objects, namespace)
	if err != nil {
		t.Fatal(err)
	}
	manager, err := resume.NewManager(lifecycle.Options{
		Namespace: namespace, Objects: objects, Identities: identities,
	})
	if err != nil {
		t.Fatal(err)
	}
	service, err := environmentservice.New(namespace, objects, identities, runtimeconfig.Catalog{}, manager, environmentservice.Options{
		ServiceLaunchers: sets.New(peertls.ActionsURI),
	})
	if err != nil {
		t.Fatal(err)
	}
	methods, err := environmentservice.WithServiceCallers(environmentservice.Methods(nil), nil, nil, []string{peertls.ActionsURI}, nil)
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.ServeTLS(t, peertls.SandboxControllerURI, methods, func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, service)
	}, peertls.ActionsURI)
	return environmentpb.NewEnvironmentServiceClient(grpcmethods.AsCaller(connection, peertls.ActionsURI))
}
