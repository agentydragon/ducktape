// Package servicefixture composes production EnvironmentService handlers for consumer tests.
package servicefixture

import (
	"testing"

	environmentservice "github.com/agentydragon/ducktape/agentplane/environments/internal/service"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"google.golang.org/grpc"
	"k8s.io/apimachinery/pkg/util/sets"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Launcher returns the production service and method authorization for a consumer's test server.
// It exposes only the wire interface: consumers own their transport and fake token reviewer,
// while EnvironmentService owns its handlers, guarded resume and management authorization.
func Launcher(t *testing.T, namespace string, objects client.WithWatch, resolver *auth.Resolver, launchers ...auth.Account) (environmentpb.EnvironmentServiceServer, []grpc.ServerOption) {
	t.Helper()
	manager, err := lifecycle.NewEnvironmentServiceManager(lifecycle.EnvironmentServiceOptions{
		Namespace: namespace, Objects: objects,
	})
	if err != nil {
		t.Fatal(err)
	}
	service, err := environmentservice.New(namespace, objects, runtimeconfig.Catalog{}, manager, nil, environmentservice.ManagementOptions{
		Launchers: sets.New(launchers...),
	})
	if err != nil {
		t.Fatal(err)
	}
	return service, environmentservice.Methods(resolver).ServerOptions(0)
}
