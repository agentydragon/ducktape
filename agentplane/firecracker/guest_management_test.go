package firecracker

import (
	"bufio"
	"context"
	"crypto/x509"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"

	managementpb "github.com/agentydragon/ducktape/agentplane/env/firecracker/managementpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type guestManagementService struct {
	managementpb.UnimplementedGuestManagementServer

	bootID              string
	synchronizeBootID   string
	statusError         error
	synchronizeError    error
	statusRelease       <-chan struct{}
	statusStarted       chan struct{}
	statusCanceled      chan struct{}
	synchronizeRequests chan *managementpb.SynchronizeRequest
	statusStartedOnce   sync.Once
	statusCanceledOnce  sync.Once
}

func newGuestManagementService(bootID string) *guestManagementService {
	return &guestManagementService{
		bootID:              bootID,
		synchronizeBootID:   bootID,
		synchronizeRequests: make(chan *managementpb.SynchronizeRequest, 2),
	}
}

func (service *guestManagementService) GetStatus(ctx context.Context, _ *managementpb.GetStatusRequest) (*managementpb.GetStatusResponse, error) {
	if service.statusStarted != nil {
		service.statusStartedOnce.Do(func() { close(service.statusStarted) })
	}
	if service.statusRelease != nil {
		select {
		case <-service.statusRelease:
		case <-ctx.Done():
			if service.statusCanceled != nil {
				service.statusCanceledOnce.Do(func() { close(service.statusCanceled) })
			}
			return nil, ctx.Err()
		}
	}
	if service.statusError != nil {
		return nil, service.statusError
	}
	return &managementpb.GetStatusResponse{BootId: service.bootID}, nil
}

func (service *guestManagementService) Synchronize(_ context.Context, request *managementpb.SynchronizeRequest) (*managementpb.SynchronizeResponse, error) {
	service.synchronizeRequests <- request
	if service.synchronizeError != nil {
		return nil, service.synchronizeError
	}
	return &managementpb.SynchronizeResponse{BootId: service.synchronizeBootID}, nil
}

type firecrackerTransport struct {
	context      context.Context
	cancel       context.CancelFunc
	listener     net.Listener
	upstream     string
	rejected     int32
	attempts     atomic.Int32
	badHandshake atomic.Bool
	acceptDone   chan struct{}
	handlers     sync.WaitGroup
}

func newFirecrackerTransport(t *testing.T, upstream string, rejectFirst int32) (*firecrackerTransport, string) {
	t.Helper()
	socketPath := filepath.Join(t.TempDir(), "vsock.sock")
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	transport := &firecrackerTransport{
		context:    ctx,
		cancel:     cancel,
		listener:   listener,
		upstream:   upstream,
		rejected:   rejectFirst,
		acceptDone: make(chan struct{}),
	}
	go transport.accept()
	t.Cleanup(func() {
		cancel()
		_ = listener.Close()
		<-transport.acceptDone
		transport.handlers.Wait()
	})
	return transport, socketPath
}

func (transport *firecrackerTransport) accept() {
	defer close(transport.acceptDone)
	for {
		connection, err := transport.listener.Accept()
		if err != nil {
			return
		}
		transport.handlers.Add(1)
		go func() {
			defer transport.handlers.Done()
			transport.handle(connection)
		}()
	}
}

func (transport *firecrackerTransport) handle(connection net.Conn) {
	defer connection.Close()
	stopClose := context.AfterFunc(transport.context, func() { _ = connection.Close() })
	defer stopClose()

	request, err := bufio.NewReader(connection).ReadString('\n')
	if err != nil {
		return
	}
	if request == "CONNECT 53\n" {
		_, _ = io.WriteString(connection, "ERR no credential subscriber in management fixture\n")
		return
	}
	if request != "CONNECT 52\n" {
		transport.badHandshake.Store(true)
		return
	}
	if transport.attempts.Add(1) <= transport.rejected {
		_, _ = io.WriteString(connection, "ERR guest is still booting\n")
		return
	}

	upstream, err := net.Dial("tcp", transport.upstream)
	if err != nil {
		return
	}
	defer upstream.Close()
	stopCloseUpstream := context.AfterFunc(transport.context, func() {
		_ = connection.Close()
		_ = upstream.Close()
	})
	defer stopCloseUpstream()
	if _, err := io.WriteString(connection, "OK 52\n"); err != nil {
		return
	}

	copyDone := make(chan struct{}, 2)
	go func() {
		_, _ = io.Copy(upstream, connection)
		copyDone <- struct{}{}
	}()
	go func() {
		_, _ = io.Copy(connection, upstream)
		copyDone <- struct{}{}
	}()
	<-copyDone
	_ = connection.Close()
	_ = upstream.Close()
	<-copyDone
}

func startGuestManagementFixture(t *testing.T, service *guestManagementService, rejectFirst int32) (*firecrackerTransport, string, string) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	managementpb.RegisterGuestManagementServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)

	transport, socketPath := newFirecrackerTransport(t, listener.Addr().String(), rejectFirst)
	credentialsDir := filepath.Join(t.TempDir(), "credentials")
	if err := os.Mkdir(credentialsDir, 0o700); err != nil {
		t.Fatal(err)
	}
	ca := transporttesting.NewCA(t)
	tls := ca.IssueWithUsage(t, "spiffe://agentplane/environment-runtime/agent-ns/sandbox-uid/pod-uid", x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth)
	for name, path := range map[string]string{"tls.crt": tls.CertFile, "tls.key": tls.KeyFile, "ca.crt": tls.CAFile} {
		value, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(credentialsDir, name), value, 0600); err != nil {
			t.Fatal(err)
		}
	}

	return transport, socketPath, credentialsDir
}

func assertNoSynchronizeRequest(t *testing.T, service *guestManagementService) {
	t.Helper()
	select {
	case request := <-service.synchronizeRequests:
		t.Fatalf("Synchronize received credentials before validation: %#v", request)
	default:
	}
}

func waitForSignal(t *testing.T, signal <-chan struct{}) {
	t.Helper()
	select {
	case <-signal:
	case <-time.After(3 * time.Second):
		t.Fatal("guest management service did not receive GetStatus")
	}
}

func TestConnectGuestWaitsForStartupAndUsesDirectVsock(t *testing.T) {
	const bootID = "guest-boot-test"
	service := newGuestManagementService(bootID)
	transport, socketPath, credentialsDir := startGuestManagementFixture(t, service, 1)
	before := time.Now().UnixNano()

	guest, err := ConnectGuest(t.Context(), socketPath, bootID, credentialsDir)
	if err != nil {
		t.Fatal(err)
	}
	defer guest.Close()
	if guest.BootID != bootID {
		t.Fatalf("boot ID = %q, want %q", guest.BootID, bootID)
	}
	if transport.attempts.Load() < 2 {
		t.Fatalf("vsock handshake attempts = %d, want a rejected startup attempt and a successful retry", transport.attempts.Load())
	}
	if transport.badHandshake.Load() {
		t.Fatal("client sent an unexpected Firecracker vsock handshake")
	}
	select {
	case request := <-service.synchronizeRequests:

		if request.GetExpectedBootId() != bootID {
			t.Fatalf("expected boot ID = %q, want %q", request.GetExpectedBootId(), bootID)
		}
		if request.GetUnixNano() < before || request.GetUnixNano() > time.Now().UnixNano() {
			t.Fatalf("host timestamp %d was outside the call interval", request.GetUnixNano())
		}
	default:
		t.Fatal("guest did not receive Synchronize")
	}
	assertNoSynchronizeRequest(t, service)
}

func TestConnectGuestValidatesStatusIdentityBeforeSendingCredentials(t *testing.T) {
	for _, test := range []struct {
		name           string
		statusBootID   string
		expectedBootID string
	}{
		{name: "missing guest identity"},
		{name: "checkpoint identity mismatch", statusBootID: "restored-guest", expectedBootID: "checkpoint-guest"},
	} {
		t.Run(test.name, func(t *testing.T) {
			service := newGuestManagementService(test.statusBootID)
			_, socketPath, credentialsDir := startGuestManagementFixture(t, service, 0)
			if _, err := ConnectGuest(t.Context(), socketPath, test.expectedBootID, credentialsDir); err == nil {
				t.Fatal("ConnectGuest succeeded with an invalid guest identity")
			}
			assertNoSynchronizeRequest(t, service)
		})
	}
}

func TestConnectGuestDoesNotSendCredentialsWhenStatusFails(t *testing.T) {
	service := newGuestManagementService("guest-boot-test")
	service.statusError = status.Error(codes.FailedPrecondition, "guest not ready")
	_, socketPath, credentialsDir := startGuestManagementFixture(t, service, 0)

	if _, err := ConnectGuest(t.Context(), socketPath, "", credentialsDir); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("GetStatus error = %v, want FailedPrecondition", err)
	}
	assertNoSynchronizeRequest(t, service)
}

func TestConnectGuestDeadlineDoesNotSendCredentials(t *testing.T) {
	service := newGuestManagementService("guest-boot-test")
	service.statusStarted = make(chan struct{})
	service.statusCanceled = make(chan struct{})
	service.statusRelease = make(chan struct{})
	_, socketPath, credentialsDir := startGuestManagementFixture(t, service, 0)
	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	result := make(chan error, 1)
	go func() {
		_, err := ConnectGuest(ctx, socketPath, "", credentialsDir)
		result <- err
	}()

	waitForSignal(t, service.statusStarted)
	if err := <-result; status.Code(err) != codes.DeadlineExceeded {
		t.Fatalf("ConnectGuest error = %v, want DeadlineExceeded", err)
	}
	waitForSignal(t, service.statusCanceled)
	assertNoSynchronizeRequest(t, service)
}

func TestConnectGuestCancellationDoesNotSendCredentials(t *testing.T) {
	service := newGuestManagementService("guest-boot-test")
	service.statusStarted = make(chan struct{})
	service.statusCanceled = make(chan struct{})
	service.statusRelease = make(chan struct{})
	_, socketPath, credentialsDir := startGuestManagementFixture(t, service, 0)
	ctx, cancel := context.WithCancel(t.Context())
	result := make(chan error, 1)
	go func() {
		_, err := ConnectGuest(ctx, socketPath, "", credentialsDir)
		result <- err
	}()

	waitForSignal(t, service.statusStarted)
	cancel()
	if err := <-result; !errors.Is(err, context.Canceled) && status.Code(err) != codes.Canceled {
		t.Fatalf("ConnectGuest error = %v, want Canceled", err)
	}
	waitForSignal(t, service.statusCanceled)
	assertNoSynchronizeRequest(t, service)
}

func TestConnectGuestRejectsChangedReplyIdentity(t *testing.T) {
	service := newGuestManagementService("guest-boot-test")
	service.synchronizeBootID = "different-guest-boot"
	_, socketPath, credentialsDir := startGuestManagementFixture(t, service, 0)

	if _, err := ConnectGuest(t.Context(), socketPath, "", credentialsDir); err == nil {
		t.Fatal("ConnectGuest accepted a changed boot ID")
	}
	select {
	case request := <-service.synchronizeRequests:
		if request.GetExpectedBootId() != service.bootID {
			t.Fatalf("expected boot ID = %q, want %q", request.GetExpectedBootId(), service.bootID)
		}
	default:
		t.Fatal("guest did not receive the single Synchronize request")
	}
	assertNoSynchronizeRequest(t, service)
}

func TestConnectGuestDoesNotRetryFailedSynchronization(t *testing.T) {
	service := newGuestManagementService("guest-boot-test")
	service.synchronizeError = status.Error(codes.Unavailable, "guest synchronization failed")
	_, socketPath, credentialsDir := startGuestManagementFixture(t, service, 0)

	if _, err := ConnectGuest(t.Context(), socketPath, "", credentialsDir); status.Code(err) != codes.Unavailable {
		t.Fatalf("Synchronize error = %v, want Unavailable", err)
	}
	select {
	case <-service.synchronizeRequests:
	default:
		t.Fatal("guest did not receive Synchronize")
	}
	assertNoSynchronizeRequest(t, service)
}
