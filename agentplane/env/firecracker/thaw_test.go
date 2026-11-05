package firecracker

import (
	"context"
	"crypto/x509"
	"errors"
	"net"
	"os"
	"reflect"
	"testing"
	"time"

	managementpb "github.com/agentydragon/ducktape/agentplane/env/firecracker/managementpb"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"github.com/agentydragon/ducktape/agentplane/firecracker/workloadidentity"
	"github.com/spiffe/go-spiffe/v2/proto/spiffe/workload"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

func managementClient(t *testing.T, service managementpb.GuestManagementServer) managementpb.GuestManagementClient {
	t.Helper()
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer(grpc.MaxRecvMsgSize(maxManagementMessageBytes))
	managementpb.RegisterGuestManagementServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient("passthrough:///guest-management-test",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return managementpb.NewGuestManagementClient(connection)
}

func TestSynchronizeRepairsClockThenRefreshesCredentialsOnEveryThaw(t *testing.T) {
	var events []string
	service := &managementServer{
		bootID: "boot", setClock: func(value time.Time) error {
			if value.UnixNano() != 123 {
				t.Errorf("unexpected clock %v", value)
			}
			events = append(events, "clock")
			return nil
		}, refresh: func(context.Context) error { events = append(events, "credentials"); return nil },
		onFirstSync: func() error { events = append(events, "ready"); return nil },
	}
	client := managementClient(t, service)
	for range 2 {
		response, err := client.Synchronize(t.Context(), &managementpb.SynchronizeRequest{ExpectedBootId: "boot", UnixNano: 123})
		if err != nil {
			t.Fatal(err)
		}
		if response.BootId != "boot" {
			t.Fatal(response)
		}
	}
	if !reflect.DeepEqual(events, []string{"clock", "credentials", "ready", "clock", "credentials"}) {
		t.Fatal(events)
	}
}

func TestSynchronizeRejectsInvalidBootOrClockBeforeMutation(t *testing.T) {
	service := &managementServer{
		bootID: "boot", setClock: func(time.Time) error { t.Error("mutated clock"); return nil },
		refresh: func(context.Context) error { t.Error("delivered credentials"); return nil }, onFirstSync: func() error { t.Error("readied"); return nil },
	}
	client := managementClient(t, service)
	for _, test := range []struct {
		request *managementpb.SynchronizeRequest
		code    codes.Code
	}{
		{&managementpb.SynchronizeRequest{ExpectedBootId: "other", UnixNano: 123}, codes.FailedPrecondition},
		{&managementpb.SynchronizeRequest{ExpectedBootId: "boot"}, codes.InvalidArgument},
	} {
		if _, err := client.Synchronize(t.Context(), test.request); status.Code(err) != test.code {
			t.Fatalf("got %v, want %v", err, test.code)
		}
	}
}

func TestSynchronizeCannotPublishReadyWithoutFreshCredentials(t *testing.T) {
	service := &managementServer{
		bootID: "boot", setClock: func(time.Time) error { return nil },
		refresh: func(context.Context) error { return errors.New("no host credentials") }, onFirstSync: func() error { t.Error("readied without credentials"); return nil },
	}
	_, err := managementClient(t, service).Synchronize(t.Context(), &managementpb.SynchronizeRequest{ExpectedBootId: "boot", UnixNano: 123})
	if status.Code(err) != codes.Unavailable {
		t.Fatal(err)
	}
}

func TestVsockAcceptsOnlyTheHostCID(t *testing.T) {
	if !isAllowedPeerCID(2) {
		t.Fatal("rejected host CID")
	}
	for _, cid := range []uint32{0, 3, 10} {
		if isAllowedPeerCID(cid) {
			t.Fatalf("accepted CID %d", cid)
		}
	}
}

func TestSubscriberFollowsRotationAndRefreshesAfterSuspension(t *testing.T) {
	ca := transporttesting.NewCA(t)
	response := func() *workload.X509SVIDResponse {
		tls := ca.IssueWithUsage(t, "spiffe://agentplane/environment-runtime/agent-ns/sandbox-uid/pod-uid", x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth)
		read := func(path string) []byte {
			value, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			return value
		}
		value, err := workloadidentity.ParsePEM(read(tls.CertFile), read(tls.KeyFile), read(tls.CAFile))
		if err != nil {
			t.Fatal(err)
		}
		return value
	}
	host := workloadidentity.New()
	first := response()
	if err := host.Set(first); err != nil {
		t.Fatal(err)
	}
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer()
	workload.RegisterSpiffeWorkloadAPIServer(server, host)
	go func() { _ = server.Serve(listener) }()
	defer server.Stop()
	subscriber := &credentialSubscriber{ctx: t.Context(), mirror: workloadidentity.New(), dial: func(ctx context.Context, _ string) (net.Conn, error) { return listener.DialContext(ctx) }}
	defer subscriber.Close()
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	if err := subscriber.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	assertCurrent := func(want *workload.X509SVIDResponse) {
		t.Helper()
		ticker := time.NewTicker(10 * time.Millisecond)
		defer ticker.Stop()
		for {
			got, err := subscriber.mirror.Snapshot()
			if err == nil && reflect.DeepEqual(got.Svids[0].X509Svid, want.Svids[0].X509Svid) {
				return
			}
			select {
			case <-ctx.Done():
				t.Fatalf("did not receive current credential: %v", err)
			case <-ticker.C:
			}
		}
	}
	assertCurrent(first)
	second := response()
	if err := host.Set(second); err != nil {
		t.Fatal(err)
	}
	assertCurrent(second)
	subscriber.Close()
	third := response()
	if err := host.Set(third); err != nil {
		t.Fatal(err)
	}
	if err := subscriber.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	assertCurrent(third)
}
