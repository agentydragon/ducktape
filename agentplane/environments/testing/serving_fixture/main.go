// This fixture runs the production every-replica listener without leader-only reconcilers.
// App tests own their Sandbox status and Runtime process; full manager coverage lives in k3s.
package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/environments/internal/serving"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/kubeclient"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
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
	callers := split(os.Getenv("ENVIRONMENTS_TEST_CALLERS"))
	var runtimeProxy *serving.RuntimeProxyConfig
	if certFile := os.Getenv("ENVIRONMENTS_TEST_RUNTIME_PROXY_CERT"); certFile != "" {
		runtimeProxy = &serving.RuntimeProxyConfig{
			RunnerSecurity: peertls.Security{
				Mode: peertls.MutualTLS,
				TLS: &peertls.Config{
					CertFile: certFile,
					KeyFile:  os.Getenv("ENVIRONMENTS_TEST_RUNTIME_PROXY_KEY"),
					CAFile:   os.Getenv("ENVIRONMENTS_TEST_RUNTIME_PROXY_CA"),
				},
			},
			Readers:           split(os.Getenv("ENVIRONMENTS_TEST_RUNTIME_READERS")),
			SessionCallers:    split(os.Getenv("ENVIRONMENTS_TEST_RUNTIME_SESSION_CALLERS")),
			ControllerCallers: split(os.Getenv("ENVIRONMENTS_TEST_RUNTIME_CONTROLLER_CALLERS")),
			ExecutorCallers:   split(os.Getenv("ENVIRONMENTS_TEST_RUNTIME_EXECUTOR_CALLERS")),
		}
	}
	server, err := serving.New(serving.Config{
		PolicyNamespace: namespace,
		RPC: listener.Config{
			ListenAddress: os.Getenv("ENVIRONMENTS_TEST_LISTEN_ADDRESS"),
			TLS:           &listener.TLS{CertFile: os.Getenv("ENVIRONMENTS_TEST_CERT"), KeyFile: os.Getenv("ENVIRONMENTS_TEST_KEY")},
			Auth:          []*listener.SPIFFEAuth{{CAFile: os.Getenv("ENVIRONMENTS_TEST_CA")}},
		},
		ServiceReaders: callers, ServiceManagers: callers, ServiceOperators: callers,
		RuntimeProxy: runtimeProxy,
	}, namespace, config, scheme, reader.KubernetesIdentities{})
	if err != nil {
		return err
	}
	return server.Start(ctx)
}

func split(value string) []string {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	var values []string
	for _, item := range strings.Split(value, ",") {
		if item = strings.TrimSpace(item); item != "" {
			values = append(values, item)
		}
	}
	return values
}
