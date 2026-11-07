package runtime

import (
	"net"
	"os"
	"path/filepath"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	environmentruntimetransport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"github.com/agentydragon/ducktape/agentplane/firecracker/workloadidentity"
	"github.com/spiffe/go-spiffe/v2/proto/spiffe/workload"
	"google.golang.org/grpc"
)

func TestHistoryIngressUsesRuntimeOwnedWorkloadAPISource(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	identity, err := environmentruntimetransport.EnvironmentRuntimeURI("test", "sandbox-uid", "test-credential")
	if err != nil {
		t.Fatal(err)
	}
	material := ca.Issue(t, identity)
	read := func(path string) []byte {
		value, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		return value
	}
	response, err := workloadidentity.ParsePEM(read(material.CertFile), read(material.KeyFile), read(material.CAFile))
	if err != nil {
		t.Fatal(err)
	}
	source := workloadidentity.New()
	if err := source.Set(response); err != nil {
		t.Fatal(err)
	}
	socket := filepath.Join(t.TempDir(), "workload.sock")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	workload.RegisterSpiffeWorkloadAPIServer(server, source)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { server.Stop(); _ = listener.Close() })
	config := environmentruntimetransport.Security{
		Mode: environmentruntimetransport.MutualTLS,
		TLS:  &environmentruntimetransport.Config{WorkloadAPISocket: "unix://" + socket},
	}
	opened, err := config.Open(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := opened.Close(); err != nil {
			t.Error(err)
		}
	})
	// History construction must reuse the source that Runtime opened for control TLS.
	runner := &Runtime{security: opened, config: Config{Control: configuration.EnvironmentRuntimeControlConfiguration{Security: config}}}
	supervisor, err := runner.ingressSupervisor(configuration.Endpoint{
		Target: "history.test:8443",
		SVID:   &configuration.SVIDEndpoint{ServerURI: environmentruntimetransport.HistoryIngressURI, ProxyAddress: "127.0.0.1:18080"},
	}, func(error) {})
	if err != nil {
		t.Fatal(err)
	}
	if supervisor == nil {
		t.Fatal("History ingress supervisor is nil")
	}
}
