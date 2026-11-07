package vsock

import (
	"context"
	"errors"
	"io"
	"net"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

func TestDialListenerRetriesAndReconnectsAfterPeerReset(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()

	var dialCount atomic.Int32
	peers := make(chan net.Conn, 2)
	listener := DialListener(ctx, func(context.Context) (net.Conn, error) {
		switch dialCount.Add(1) {
		case 1:
			return nil, errors.New("guest port is not ready")
		default:
			connection, peer := net.Pipe()
			peers <- peer
			return connection, nil
		}
	})
	t.Cleanup(func() { _ = listener.Close() })

	first := acceptWithin(t, listener)
	firstPeer := <-peers
	if got := dialCount.Load(); got < 2 {
		t.Fatalf("dial attempts = %d, want retry after first failure", got)
	}

	readDone := make(chan error, 1)
	go func() {
		var buffer [1]byte
		_, err := first.Read(buffer[:])
		readDone <- err
	}()

	secondAccept := make(chan net.Conn, 1)
	secondErr := make(chan error, 1)
	go func() {
		connection, err := listener.Accept()
		if err != nil {
			secondErr <- err
			return
		}
		secondAccept <- connection
	}()

	select {
	case <-time.After(50 * time.Millisecond):
	case connection := <-secondAccept:
		_ = connection.Close()
		t.Fatal("listener dialed again while the first stream was still open")
	case err := <-secondErr:
		t.Fatalf("second Accept returned before the first stream closed: %v", err)
	}
	if got := dialCount.Load(); got != 2 {
		t.Fatalf("dial attempts while first stream was open = %d, want 2", got)
	}

	if err := firstPeer.Close(); err != nil {
		t.Fatal(err)
	}
	if err := <-readDone; !errors.Is(err, io.EOF) {
		t.Fatalf("read after peer reset = %v, want EOF", err)
	}
	second := receiveConn(t, secondAccept, secondErr)
	defer second.Close()
	secondPeer := <-peers
	if got := dialCount.Load(); got != 3 {
		t.Fatalf("dial attempts after peer reset = %d, want 3", got)
	}
	_ = listener.Close()
	_ = secondPeer.SetReadDeadline(time.Now().Add(time.Second))
	var buffer [1]byte
	if _, err := secondPeer.Read(buffer[:]); err == nil {
		t.Fatal("peer remained open after listener Close")
	}
}

func TestDialListenerCloseInterruptsInFlightDial(t *testing.T) {
	entered := make(chan struct{})
	listener := DialListener(t.Context(), func(ctx context.Context) (net.Conn, error) {
		close(entered)
		<-ctx.Done()
		return nil, ctx.Err()
	})

	accepted := make(chan error, 1)
	go func() {
		_, err := listener.Accept()
		accepted <- err
	}()
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("dial function was not called")
	}
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-accepted:
		if !errors.Is(err, net.ErrClosed) {
			t.Fatalf("Accept error = %v, want net.ErrClosed", err)
		}
	case <-time.After(time.Second):
		t.Fatal("Close did not interrupt the in-flight dial")
	}
}

func TestDialListenerContextStopsRepeatedDialFailures(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 75*time.Millisecond)
	defer cancel()
	var dialCount atomic.Int32
	listener := DialListener(ctx, func(context.Context) (net.Conn, error) {
		dialCount.Add(1)
		return nil, errors.New("guest is restarting")
	})

	_, err := listener.Accept()
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Accept error = %v, want context deadline", err)
	}
	if got := dialCount.Load(); got < 2 || got > 10 {
		t.Fatalf("dial attempts before deadline = %d, want retries with bounded backoff", got)
	}
}

func TestAcceptDialerCanceledRequestDoesNotBlockNextConnection(t *testing.T) {
	listener := newChannelListener()
	broker := AcceptDialer(listener)
	t.Cleanup(func() { _ = broker.Close() })

	ctx, cancel := context.WithCancel(t.Context())
	canceledResult := make(chan error, 1)
	go func() {
		_, err := broker.DialContext(ctx, "ignored")
		canceledResult <- err
	}()
	waitUntil(t, func() bool { return len(broker.requests) == 1 })
	cancel()
	if err := <-canceledResult; !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled DialContext error = %v, want context.Canceled", err)
	}

	firstGuest, firstHost := net.Pipe()
	if err := listener.push(firstGuest); err != nil {
		t.Fatal(err)
	}
	_ = firstHost.SetReadDeadline(time.Now().Add(time.Second))
	var buffer [1]byte
	if _, err := firstHost.Read(buffer[:]); !errors.Is(err, io.EOF) {
		t.Fatalf("canceled request stream read = %v, want EOF", err)
	}

	nextCtx, nextCancel := context.WithTimeout(t.Context(), time.Second)
	defer nextCancel()
	nextResult := make(chan net.Conn, 1)
	nextError := make(chan error, 1)
	go func() {
		connection, err := broker.DialContext(nextCtx, "also-ignored")
		if err != nil {
			nextError <- err
			return
		}
		nextResult <- connection
	}()
	waitUntil(t, func() bool { return len(broker.requests) == 1 })

	secondGuest, secondHost := net.Pipe()
	defer secondHost.Close()
	if err := listener.push(secondGuest); err != nil {
		t.Fatal(err)
	}
	connection := receiveConn(t, nextResult, nextError)
	defer connection.Close()
	writeError := make(chan error, 1)
	go func() {
		_, err := connection.Write([]byte("next"))
		writeError <- err
	}()
	got := make([]byte, 4)
	if _, err := io.ReadFull(secondHost, got); err != nil {
		t.Fatal(err)
	}
	if err := <-writeError; err != nil {
		t.Fatal(err)
	}
	if string(got) != "next" {
		t.Fatalf("transferred bytes = %q, want next", got)
	}
}

func TestReverseAdaptersCarryGRPCHealthRequest(t *testing.T) {
	guestListener := newChannelListener()
	broker := AcceptDialer(guestListener)
	t.Cleanup(func() { _ = broker.Close() })

	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	listener := DialListener(ctx, func(context.Context) (net.Conn, error) {
		host, guest := net.Pipe()
		if err := guestListener.push(guest); err != nil {
			_ = host.Close()
			return nil, err
		}
		return host, nil
	})

	server := grpc.NewServer()
	healthServer := health.NewServer()
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(server, healthServer)
	serveResult := make(chan error, 1)
	go func() { serveResult <- server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
		select {
		case <-serveResult:
		case <-time.After(time.Second):
			t.Error("gRPC server did not stop")
		}
	})

	client, err := grpc.NewClient(
		"passthrough:///reverse-workload-api",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(broker.DialContext),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })

	rpcCtx, rpcCancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer rpcCancel()
	response, err := healthpb.NewHealthClient(client).Check(rpcCtx, &healthpb.HealthCheckRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if response.GetStatus() != healthpb.HealthCheckResponse_SERVING {
		t.Fatalf("health status = %s, want SERVING", response.GetStatus())
	}
}

func acceptWithin(t *testing.T, listener net.Listener) net.Conn {
	t.Helper()
	accepted := make(chan net.Conn, 1)
	acceptError := make(chan error, 1)
	go func() {
		connection, err := listener.Accept()
		if err != nil {
			acceptError <- err
			return
		}
		accepted <- connection
	}()
	return receiveConn(t, accepted, acceptError)
}

func receiveConn(t *testing.T, accepted <-chan net.Conn, acceptError <-chan error) net.Conn {
	t.Helper()
	select {
	case connection := <-accepted:
		return connection
	case err := <-acceptError:
		t.Fatalf("Accept returned an error: %v", err)
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for a connection")
	}
	return nil
}

func waitUntil(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for !condition() {
		if time.Now().After(deadline) {
			t.Fatal("condition did not become true")
		}
		time.Sleep(time.Millisecond)
	}
}

type channelListener struct {
	connections chan net.Conn
	done        chan struct{}
	closeOnce   atomic.Bool
}

func newChannelListener() *channelListener {
	return &channelListener{
		connections: make(chan net.Conn, 1),
		done:        make(chan struct{}),
	}
}

func (l *channelListener) Accept() (net.Conn, error) {
	select {
	case connection := <-l.connections:
		return connection, nil
	case <-l.done:
		return nil, net.ErrClosed
	}
}

func (l *channelListener) Close() error {
	if l.closeOnce.CompareAndSwap(false, true) {
		close(l.done)
	}
	return nil
}

func (l *channelListener) Addr() net.Addr {
	return reverseAddr{}
}

func (l *channelListener) push(connection net.Conn) error {
	select {
	case l.connections <- connection:
		return nil
	case <-l.done:
		return net.ErrClosed
	}
}
