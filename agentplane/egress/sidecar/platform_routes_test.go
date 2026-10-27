package main

import (
	"context"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/keepalive"
)

func TestPlatformRouteRecoversAfterSilentUpstreamBlackhole(t *testing.T) {
	original := startRouteTestBackend(t, "original")
	replacement := startRouteTestBackend(t, "replacement")
	proxy := startRouteTestProxy(t, original)
	routes, err := bindPlatformRoutesWithKeepalive(
		map[string]platformRouteConfig{
			"control": {ListenAddress: "127.0.0.1:0", Upstream: proxy.listener.Addr().String()},
		},
		keepalive.ClientParameters{Time: 10 * time.Second, Timeout: time.Second},
	)
	if err != nil {
		t.Fatal(err)
	}
	serveDone := make(chan error, 1)
	go func() { serveDone <- routes[0].server.Serve(routes[0].listen) }()
	t.Cleanup(func() {
		stopPlatformRoutes(routes)
		<-serveDone
	})

	client, err := grpc.NewClient(routes[0].listen.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
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
