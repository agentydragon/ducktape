package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"net"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
)

func TestDispatcherProxyPreservesBidiRPCAndMetadata(t *testing.T) {
	ca := transporttesting.NewCA(t)
	serverCredentials := ca.IssueWithUsage(
		t,
		environment_runtime_transport.SessionDispatcherURI,
		x509.ExtKeyUsageServerAuth,
	)
	var authorization atomic.Value
	backend := startProxyTestBackend(t, serverCredentials, func(stream grpc.ServerStream) error {
		incoming, _ := metadata.FromIncomingContext(stream.Context())
		authorization.Store(incoming.Get("authorization"))
		if err := stream.SendHeader(metadata.Pairs("x-upstream-header", "forwarded")); err != nil {
			return err
		}
		for {
			var message []byte
			if err := stream.RecvMsg(&message); err != nil {
				if !errors.Is(err, io.EOF) {
					return err
				}
				stream.SetTrailer(metadata.Pairs("x-upstream-trailer", "forwarded"))
				return nil
			}
			if err := stream.SendMsg(append([]byte("reply:"), message...)); err != nil {
				return err
			}
		}
	})

	proxy := startProxyTestDispatcher(t, backend, ca.RootFile)
	client := proxyTestClient(t, proxy)
	ctx, cancel := context.WithTimeout(
		metadata.NewOutgoingContext(context.Background(), metadata.Pairs("authorization", "Bearer caller-token")),
		5*time.Second,
	)
	defer cancel()
	stream, err := client.NewStream(
		ctx,
		&grpc.StreamDesc{ClientStreams: true, ServerStreams: true},
		"/agentplane.test.Dispatcher/Exchange",
		grpc.ForceCodec(rawCodec{}),
	)
	if err != nil {
		t.Fatal(err)
	}
	for _, message := range []string{"first", "second"} {
		if err := stream.SendMsg([]byte(message)); err != nil {
			t.Fatalf("send %q: %v", message, err)
		}
	}
	if err := stream.CloseSend(); err != nil {
		t.Fatalf("half-close request stream: %v", err)
	}
	for _, want := range []string{"reply:first", "reply:second"} {
		var response []byte
		if err := stream.RecvMsg(&response); err != nil || string(response) != want {
			t.Fatalf("response = %q, %v; want %q", response, err, want)
		}
	}
	var finalResponse []byte
	if err := stream.RecvMsg(&finalResponse); !errors.Is(err, io.EOF) {
		t.Fatalf("final response error = %v, want EOF", err)
	} else if len(finalResponse) != 0 {
		t.Fatalf("unexpected final response %q", finalResponse)
	}
	if values := authorization.Load().([]string); len(values) != 1 || values[0] != "Bearer caller-token" {
		t.Fatalf("upstream authorization = %v", values)
	}
	if header, err := stream.Header(); err != nil || len(header.Get("x-upstream-header")) != 1 {
		t.Fatalf("response header = %v, %v", header, err)
	}
	if trailers := stream.Trailer(); len(trailers.Get("x-upstream-trailer")) != 1 {
		t.Fatalf("response trailers = %v", trailers)
	}
}

func TestDispatcherProxyRejectsTrustedCertificateWithWrongSPIFFEURI(t *testing.T) {
	ca := transporttesting.NewCA(t)
	wrongServer := ca.IssueWithUsage(
		t,
		environment_runtime_transport.NotificationsURI,
		x509.ExtKeyUsageServerAuth,
	)
	var calls atomic.Int32
	backend := startProxyTestBackend(t, wrongServer, func(grpc.ServerStream) error {
		calls.Add(1)
		return nil
	})
	proxy := startProxyTestDispatcher(t, backend, ca.RootFile)
	client := proxyTestClient(t, proxy)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	var response []byte
	err := client.Invoke(
		ctx,
		"/agentplane.test.Dispatcher/GetSession",
		[]byte("request"),
		&response,
		grpc.ForceCodec(rawCodec{}),
	)
	if err == nil {
		t.Fatal("proxy accepted a trusted server with the wrong SPIFFE identity")
	}
	if got := calls.Load(); got != 0 {
		t.Fatalf("wrong-identity backend calls = %d, want 0", got)
	}
}

func TestValidatePortForwardTargetRequiresLoopbackIP(t *testing.T) {
	for _, target := range []string{"dispatcher:8080", "192.0.2.1:8080", "127.0.0.1:0", "127.0.0.1:not-a-port"} {
		t.Run(target, func(t *testing.T) {
			if err := validatePortForwardTarget(target); err == nil {
				t.Fatalf("validatePortForwardTarget(%q) succeeded", target)
			}
		})
	}
	if err := validatePortForwardTarget("127.0.0.1:443"); err != nil {
		t.Fatalf("valid loopback target rejected: %v", err)
	}
}

func startProxyTestBackend(
	t *testing.T,
	config environment_runtime_transport.Config,
	handler func(grpc.ServerStream) error,
) string {
	t.Helper()
	pair, err := tls.LoadX509KeyPair(config.CertFile, config.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(
		grpc.Creds(credentials.NewTLS(&tls.Config{Certificates: []tls.Certificate{pair}})),
		grpc.ForceServerCodec(rawCodec{}),
		grpc.UnknownServiceHandler(func(_ any, stream grpc.ServerStream) error { return handler(stream) }),
	)
	serveDone := make(chan error, 1)
	go func() { serveDone <- server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
		if err := <-serveDone; err != nil && err != grpc.ErrServerStopped {
			t.Errorf("test backend stopped with error: %v", err)
		}
	})
	return listener.Addr().String()
}

func startProxyTestDispatcher(t *testing.T, target, caFile string) *dispatcherProxy {
	t.Helper()
	proxy, err := newDispatcherProxy(target, caFile)
	if err != nil {
		t.Fatal(err)
	}
	serveDone := make(chan error, 1)
	go func() { serveDone <- proxy.server.Serve(proxy.listener) }()
	t.Cleanup(func() {
		proxy.close()
		if err := <-serveDone; err != nil && err != grpc.ErrServerStopped {
			t.Errorf("proxy stopped with error: %v", err)
		}
	})
	return proxy
}

func proxyTestClient(t *testing.T, proxy *dispatcherProxy) *grpc.ClientConn {
	t.Helper()
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(proxy.rootCertificates) {
		t.Fatal("proxy did not provide a valid local trust root")
	}
	client, err := grpc.NewClient(
		proxy.listener.Addr().String(),
		grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{RootCAs: roots, ServerName: "localhost"})),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	return client
}
