package stream

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"testing"
	"time"

	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

// DNS and address admission belong to the central proxy. The workload must
// preserve the configured authority rather than CONNECTing to a locally resolved IP.
func TestTunneledGRPCPreservesAuthorityAndVerifiesDestinationTLS(t *testing.T) {
	ca := transporttesting.NewCA(t)
	config := ca.Issue(t, transport.HistoryIngressURI)
	security, err := config.WorkloadServerTLSConfig(transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	upstream, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(credentials.NewTLS(security)))
	healthServer := health.NewServer()
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(server, healthServer)
	go func() { _ = server.Serve(upstream) }()
	t.Cleanup(server.Stop)
	proxy, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer proxy.Close()
	authority := "history.not-resolvable.invalid:8082"
	finished := make(chan error, 1)
	go func() {
		incoming, err := proxy.Accept()
		if err != nil {
			finished <- err
			return
		}
		defer incoming.Close()
		reader := bufio.NewReader(incoming)
		request, err := http.ReadRequest(reader)
		if err != nil {
			finished <- err
			return
		}
		if request.Host != authority {
			finished <- fmt.Errorf("CONNECT target=%q", request.Host)
			return
		}
		remote, err := net.Dial("tcp", upstream.Addr().String())
		if err != nil {
			finished <- err
			return
		}
		defer remote.Close()
		if _, err := io.WriteString(incoming, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
			finished <- err
			return
		}
		copied := make(chan struct{}, 2)
		go func() { _, _ = io.Copy(remote, reader); copied <- struct{}{} }()
		go func() { _, _ = io.Copy(incoming, remote); copied <- struct{}{} }()
		<-copied
		_ = incoming.Close()
		_ = remote.Close()
		<-copied
		finished <- nil
	}()
	runtime := ca.Issue(t, "spiffe://agentplane/environment-runtime/test/sandbox/credential")
	clientSecurity, err := runtime.ClientCredentials(transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	connection, err := NewTunneledConnection(authority, proxy.Addr().String(), clientSecurity)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	if _, err := healthpb.NewHealthClient(connection).Check(ctx, &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatal(err)
	}
	_ = connection.Close()
	select {
	case err := <-finished:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
}
