// This fixture runs the production every-replica listener without leader-only reconcilers.
// App tests own their Sandbox status and Runtime process; full manager coverage lives in k3s.
package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/environments/internal/serving"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/kubeclient"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
)

func main() {
	if err := run(); err != nil {
		log.Print(err)
		os.Exit(1)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	config, err := kubeclient.Config(os.Getenv("KUBECONFIG"))
	if err != nil {
		return err
	}
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		return err
	}
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		return err
	}
	for _, add := range []func(*runtime.Scheme) error{policyapi.AddToScheme, workloadapi.AddToScheme} {
		if err := add(scheme); err != nil {
			return err
		}
	}
	namespace := os.Getenv("ENVIRONMENTS_TEST_NAMESPACE")
	callers := []string{os.Getenv("ENVIRONMENTS_TEST_CALLER_URI")}
	server, err := serving.New(serving.Config{
		PolicyNamespace: namespace,
		Audience:        "test-environments",
		RPC: listener.Config{
			ListenAddress: os.Getenv("ENVIRONMENTS_TEST_LISTEN_ADDRESS"),
			TLS:           &listener.TLS{CertFile: os.Getenv("ENVIRONMENTS_TEST_CERT"), KeyFile: os.Getenv("ENVIRONMENTS_TEST_KEY")},
			Auth:          []listener.Authentication{&listener.SPIFFEAuth{CAFile: os.Getenv("ENVIRONMENTS_TEST_CA")}},
		},
		ServiceReaders: callers, ServiceManagers: callers, ServiceOperators: callers,
	}, namespace, config, scheme)
	if err != nil {
		return err
	}
	return server.Start(ctx)
}
