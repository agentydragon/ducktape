package main

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestBindPlatformRoutesWithoutRoutesDoesNotOpenListener(t *testing.T) {
	routes, err := bindPlatformRoutesWithKeepalive(platformRoutesConfig{}, keepalive.ClientParameters{})
	if err != nil {
		t.Fatal(err)
	}
	if routes != nil {
		t.Fatalf("empty route configuration opened listener %v", routes.listen.Addr())
	}
}

func TestPlatformRouteRecoversAfterSilentUpstreamBlackhole(t *testing.T) {
	original := startRouteTestBackend(t, "original")
	replacement := startRouteTestBackend(t, "replacement")
	proxy := startRouteTestProxy(t, original)
	routes, err := bindPlatformRoutesWithKeepalive(
		platformRoutesConfig{
			ListenAddress: "127.0.0.1:0",
			Routes: map[string]platformRouteConfig{
				"control": {ServicePrefixes: []string{"/route.test.Service/"}, Upstream: proxy.listener.Addr().String()},
			},
		},
		keepalive.ClientParameters{Time: 10 * time.Second, Timeout: time.Second},
	)
	if err != nil {
		t.Fatal(err)
	}
	serveDone := make(chan error, 1)
	go func() { serveDone <- routes.server.Serve(routes.listen) }()
	t.Cleanup(func() {
		closePlatformRoutes(routes)
		<-serveDone
	})

	client, err := grpc.NewClient(routes.listen.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })

	first, cancelFirst := newRouteTestStream(t, client)
	t.Cleanup(cancelFirst)
	if got := sendRouteTestMessage(t, first, "before-blackhole"); got != "original:before-blackhole" {
		t.Fatalf("initial response = %q", got)
	}
	oldStreamDone := make(chan error, 1)
	go func() {
		var response []byte
		oldStreamDone <- first.RecvMsg(&response)
	}()

	proxy.blackholeAndSwitch(replacement)
	select {
	case err := <-oldStreamDone:
		if err == nil {
			t.Fatal("blackholed stream ended successfully")
		}
	case <-time.After(20 * time.Second):
		t.Fatal("sidecar did not discard the silent upstream transport")
	}
	deadline := time.Now().Add(10 * time.Second)
	for {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			t.Fatal("RPC through the recovered route did not succeed before its deadline")
		}
		stream, cancel, err := newRouteTestStreamE(client, remaining)
		if err != nil {
			if time.Now().After(deadline) {
				t.Fatalf("open RPC through the recovered route: %v", err)
			}
			time.Sleep(100 * time.Millisecond)
			continue
		}
		response, err := sendRouteTestMessageE(stream, "after-blackhole")
		cancel()
		if err == nil {
			if response != "replacement:after-blackhole" {
				t.Fatalf("replacement response = %q", response)
			}
			proxy.waitForConnections(t, 2, time.Second)
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("RPC through the recovered route failed: %v", err)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func startRouteTestBackend(t *testing.T, name string) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(
		grpc.ForceServerCodec(rawCodec{}),
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{MinTime: 5 * time.Second}),
		grpc.UnknownServiceHandler(func(_ any, stream grpc.ServerStream) error {
			for {
				var request []byte
				if err := stream.RecvMsg(&request); err != nil {
					return err
				}
				if err := stream.SendMsg([]byte(name + ":" + string(request))); err != nil {
					return err
				}
			}
		}),
	)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	return listener.Addr().String()
}

type routeTestProxy struct {
	listener net.Listener
	mu       sync.Mutex
	backend  string
	tunnels  []*routeTestTunnel
	count    atomic.Int64
	done     chan struct{}
	writers  sync.WaitGroup
}

type routeTestTunnel struct {
	client    net.Conn
	backend   net.Conn
	blackhole atomic.Bool
}

func startRouteTestProxy(t *testing.T, backend string) *routeTestProxy {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	proxy := &routeTestProxy{listener: listener, backend: backend, done: make(chan struct{})}
	go proxy.accept()
	t.Cleanup(proxy.close)
	return proxy
}

func (p *routeTestProxy) accept() {
	defer close(p.done)
	for {
		client, err := p.listener.Accept()
		if err != nil {
			return
		}
		p.mu.Lock()
		backendAddress := p.backend
		p.mu.Unlock()
		backend, err := net.DialTimeout("tcp", backendAddress, 5*time.Second)
		if err != nil {
			_ = client.Close()
			continue
		}
		tunnel := &routeTestTunnel{client: client, backend: backend}
		p.mu.Lock()
		p.tunnels = append(p.tunnels, tunnel)
		p.mu.Unlock()
		p.count.Add(1)
		p.writers.Add(2)
		go p.copy(tunnel, backend, client)
		go p.copy(tunnel, client, backend)
	}
}

func (p *routeTestProxy) copy(tunnel *routeTestTunnel, source, destination net.Conn) {
	defer p.writers.Done()
	buffer := make([]byte, 32*1024)
	for {
		count, err := source.Read(buffer)
		if count > 0 && !tunnel.blackhole.Load() {
			for written := 0; written < count; {
				n, writeErr := destination.Write(buffer[written:count])
				written += n
				if writeErr != nil {
					return
				}
			}
		}
		if err != nil {
			return
		}
	}
}

func (p *routeTestProxy) blackholeAndSwitch(backend string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, tunnel := range p.tunnels {
		tunnel.blackhole.Store(true)
	}
	p.backend = backend
}

func (p *routeTestProxy) waitForConnections(t *testing.T, count int64, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for p.count.Load() < count {
		if time.Now().After(deadline) {
			t.Fatalf("upstream connections = %d, want at least %d", p.count.Load(), count)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func (p *routeTestProxy) close() {
	_ = p.listener.Close()
	<-p.done
	p.mu.Lock()
	for _, tunnel := range p.tunnels {
		_ = tunnel.client.Close()
		_ = tunnel.backend.Close()
	}
	p.mu.Unlock()
	p.writers.Wait()
}

func newRouteTestStream(t *testing.T, client *grpc.ClientConn) (grpc.ClientStream, context.CancelFunc) {
	t.Helper()
	stream, cancel, err := newRouteTestStreamE(client, 30*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	return stream, cancel
}

func newRouteTestStreamE(client *grpc.ClientConn, timeout time.Duration) (grpc.ClientStream, context.CancelFunc, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	stream, err := client.NewStream(
		ctx,
		&grpc.StreamDesc{ClientStreams: true, ServerStreams: true},
		"/route.test.Service/Exchange",
		grpc.ForceCodec(rawCodec{}),
	)
	if err != nil {
		cancel()
		return nil, nil, err
	}
	return stream, cancel, nil
}

func sendRouteTestMessage(t *testing.T, stream grpc.ClientStream, message string) string {
	t.Helper()
	response, err := sendRouteTestMessageE(stream, message)
	if err != nil {
		t.Fatal(err)
	}
	return response
}

func sendRouteTestMessageE(stream grpc.ClientStream, message string) (string, error) {
	if err := stream.SendMsg([]byte(message)); err != nil {
		return "", err
	}
	var response []byte
	if err := stream.RecvMsg(&response); err != nil {
		return "", err
	}
	return string(response), nil
}

func TestPlatformRoutesDispatchAndIsolateRoutes(t *testing.T) {
	alpha := startSharedPlatformTestBackend(t, "alpha")
	beta := startSharedPlatformTestBackend(t, "beta")
	telemetry := startSharedPlatformTestBackend(t, "telemetry")
	deadListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	deadAddress := deadListener.Addr().String()
	if err := deadListener.Close(); err != nil {
		t.Fatal(err)
	}

	alphaToken := writeSharedPlatformTestToken(t, "alpha-token")
	betaToken := writeSharedPlatformTestToken(t, "beta-token")
	shared, err := bindPlatformRoutesWithKeepalive(platformRoutesConfig{ListenAddress: "127.0.0.1:0", Routes: map[string]platformRouteConfig{
		"alpha":       {ServicePrefixes: []string{"/route.alpha.Service/"}, Upstream: alpha.address, TokenFile: alphaToken},
		"beta":        {ServicePrefixes: []string{"/route.beta.Service/"}, Upstream: beta.address, TokenFile: betaToken},
		"telemetry":   {ServicePrefixes: []string{"/route.telemetry.Service/"}, Upstream: telemetry.address},
		"unavailable": {ServicePrefixes: []string{"/route.unavailable.Service/"}, Upstream: deadAddress},
	}}, keepalive.ClientParameters{})
	if err != nil {
		t.Fatal(err)
	}
	serveDone := make(chan error, 1)
	go func() { serveDone <- shared.server.Serve(shared.listen) }()
	t.Cleanup(func() {
		closePlatformRoutes(shared)
		if err := <-serveDone; err != nil {
			t.Errorf("shared route server stopped with error: %v", err)
		}
	})

	client, err := grpc.NewClient(shared.listen.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })

	for _, test := range []struct {
		service string
		want    string
	}{
		{service: "alpha", want: "alpha:alpha-request"},
		{service: "beta", want: "beta:beta-request"},
	} {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer caller-token", "x-test-route", test.service)
		got, err := invokeSharedPlatformTestUnary(ctx, client, "/route."+test.service+".Service/Unary", test.service+"-request")
		cancel()
		if err != nil {
			t.Fatalf("%s unary call: %v", test.service, err)
		}
		if got != test.want {
			t.Fatalf("%s unary response = %q, want %q", test.service, got, test.want)
		}
		observed := receiveSharedPlatformTestObservation(t, map[string]*sharedPlatformTestBackend{
			"alpha": alpha, "beta": beta,
		}[test.service])
		wantToken := "Bearer " + test.service + "-token"
		if len(observed.authorization) != 1 || observed.authorization[0] != wantToken {
			t.Fatalf("%s upstream authorization = %v, want only %q", test.service, observed.authorization, wantToken)
		}
		if len(observed.routeMarker) != 1 || observed.routeMarker[0] != test.service {
			t.Fatalf("%s forwarded route marker = %v", test.service, observed.routeMarker)
		}
	}

	telemetryCtx, cancelTelemetry := context.WithTimeout(context.Background(), 3*time.Second)
	telemetryCtx = metadata.AppendToOutgoingContext(telemetryCtx, "authorization", "Bearer caller-token")
	if got, err := invokeSharedPlatformTestUnary(telemetryCtx, client, "/route.telemetry.Service/Unary", "event"); err != nil || got != "telemetry:event" {
		cancelTelemetry()
		t.Fatalf("telemetry unary = %q, %v", got, err)
	}
	cancelTelemetry()
	if observed := receiveSharedPlatformTestObservation(t, telemetry); len(observed.authorization) != 0 {
		t.Fatalf("unauthenticated telemetry forwarded authorization %v", observed.authorization)
	}

	streamCtx, cancelStream := context.WithTimeout(context.Background(), 3*time.Second)
	streamCtx = metadata.AppendToOutgoingContext(streamCtx, "authorization", "Bearer caller-token")
	stream, err := client.NewStream(streamCtx, &grpc.StreamDesc{ClientStreams: true, ServerStreams: true}, "/route.alpha.Service/Exchange", grpc.ForceCodec(rawCodec{}))
	if err != nil {
		cancelStream()
		t.Fatal(err)
	}
	for _, message := range []string{"first", "second"} {
		if err := stream.SendMsg([]byte(message)); err != nil {
			cancelStream()
			t.Fatalf("send bidi message %q: %v", message, err)
		}
	}
	if err := stream.CloseSend(); err != nil {
		cancelStream()
		t.Fatalf("half-close bidi stream: %v", err)
	}
	for _, want := range []string{"alpha:first", "alpha:second"} {
		var response []byte
		if err := stream.RecvMsg(&response); err != nil || string(response) != want {
			cancelStream()
			t.Fatalf("bidi response = %q, %v; want %q", response, err, want)
		}
	}
	var finalResponse []byte
	if err := stream.RecvMsg(&finalResponse); err == nil {
		cancelStream()
		t.Fatalf("bidi stream had unexpected final response %q", finalResponse)
	}
	cancelStream()
	_ = receiveSharedPlatformTestObservation(t, alpha)

	blockCtx, cancelBlock := context.WithCancel(context.Background())
	blockStream, err := client.NewStream(blockCtx, &grpc.StreamDesc{ClientStreams: true, ServerStreams: true}, "/route.alpha.Service/Block", grpc.ForceCodec(rawCodec{}))
	if err != nil {
		cancelBlock()
		t.Fatal(err)
	}
	if err := blockStream.SendMsg([]byte("wait")); err != nil {
		cancelBlock()
		t.Fatalf("start cancellable call: %v", err)
	}
	_ = receiveSharedPlatformTestObservation(t, alpha)
	waitSharedPlatformTestSignal(t, alpha.blockStarted, "blocking backend handler did not start")
	cancelBlock()
	waitSharedPlatformTestSignal(t, alpha.cancelObserved, "backend did not observe frontend cancellation")

	unknownCtx, cancelUnknown := context.WithTimeout(context.Background(), 3*time.Second)
	_, unknownErr := invokeSharedPlatformTestUnary(unknownCtx, client, "/route.unknown.Service/Unary", "ignored")
	cancelUnknown()
	if status.Code(unknownErr) != codes.Unimplemented {
		t.Fatalf("unknown service status = %v, want Unimplemented", unknownErr)
	}

	failedCtx, cancelFailed := context.WithTimeout(context.Background(), 3*time.Second)
	_, failedErr := invokeSharedPlatformTestUnary(failedCtx, client, "/route.unavailable.Service/Unary", "ignored")
	cancelFailed()
	if status.Code(failedErr) != codes.Unavailable {
		t.Fatalf("unavailable route status = %v, want Unavailable", failedErr)
	}
	healthyCtx, cancelHealthy := context.WithTimeout(context.Background(), 3*time.Second)
	got, healthyErr := invokeSharedPlatformTestUnary(healthyCtx, client, "/route.alpha.Service/Unary", "after-failure")
	cancelHealthy()
	if healthyErr != nil || got != "alpha:after-failure" {
		t.Fatalf("healthy route after isolated failure = %q, %v", got, healthyErr)
	}
}

type sharedPlatformTestBackend struct {
	address        string
	observations   chan sharedPlatformTestObservation
	blockStarted   chan struct{}
	cancelObserved chan struct{}
}

type sharedPlatformTestObservation struct {
	method        string
	authorization []string
	routeMarker   []string
}

func startSharedPlatformTestBackend(t *testing.T, name string) *sharedPlatformTestBackend {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	backend := &sharedPlatformTestBackend{
		address:        listener.Addr().String(),
		observations:   make(chan sharedPlatformTestObservation, 16),
		blockStarted:   make(chan struct{}),
		cancelObserved: make(chan struct{}),
	}
	server := grpc.NewServer(grpc.ForceServerCodec(rawCodec{}), grpc.UnknownServiceHandler(func(_ any, stream grpc.ServerStream) error {
		method, _ := grpc.MethodFromServerStream(stream)
		incoming, _ := metadata.FromIncomingContext(stream.Context())
		backend.observations <- sharedPlatformTestObservation{
			method:        method,
			authorization: append([]string(nil), incoming.Get("authorization")...),
			routeMarker:   append([]string(nil), incoming.Get("x-test-route")...),
		}
		if strings.HasSuffix(method, "/Block") {
			if err := stream.SendHeader(metadata.MD{}); err != nil {
				return err
			}
			close(backend.blockStarted)
			<-stream.Context().Done()
			close(backend.cancelObserved)
			return status.FromContextError(stream.Context().Err()).Err()
		}
		for {
			var request []byte
			if err := stream.RecvMsg(&request); err != nil {
				if err == context.Canceled || err == context.DeadlineExceeded {
					return status.FromContextError(err).Err()
				}
				return nil
			}
			if err := stream.SendMsg([]byte(name + ":" + string(request))); err != nil {
				return err
			}
			if strings.HasSuffix(method, "/Unary") {
				return nil
			}
		}
	}))
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	return backend
}

func writeSharedPlatformTestToken(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func invokeSharedPlatformTestUnary(ctx context.Context, client *grpc.ClientConn, method, request string) (string, error) {
	var response []byte
	err := client.Invoke(ctx, method, []byte(request), &response, grpc.ForceCodec(rawCodec{}))
	return string(response), err
}

func receiveSharedPlatformTestObservation(t *testing.T, backend *sharedPlatformTestBackend) sharedPlatformTestObservation {
	t.Helper()
	select {
	case observed := <-backend.observations:
		return observed
	case <-time.After(3 * time.Second):
		t.Fatal("backend did not receive the expected RPC")
		return sharedPlatformTestObservation{}
	}
}

func waitSharedPlatformTestSignal(t *testing.T, signal <-chan struct{}, failure string) {
	t.Helper()
	select {
	case <-signal:
	case <-time.After(3 * time.Second):
		t.Fatal(failure)
	}
}
