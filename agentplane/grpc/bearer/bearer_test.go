package bearer

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// blockedService is the health service whose Check never answers before its caller gives up.
const blockedService = "test-blocked"

// serverSeen records the authorization metadata of each call that reached the server.
type serverSeen struct{ authorization chan []string }

// start serves the health service and returns a client connection to it from Dial.
func start(t *testing.T, tokenPath string, timeout time.Duration) (healthpb.HealthClient, *serverSeen) {
	t.Helper()
	seen := &serverSeen{authorization: make(chan []string, 8)}
	server := grpc.NewServer(
		grpc.UnaryInterceptor(func(ctx context.Context, request any, _ *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
			seen.authorization <- metadata.ValueFromIncomingContext(ctx, "authorization")
			if request.(*healthpb.HealthCheckRequest).GetService() == blockedService {
				<-ctx.Done()
				return nil, ctx.Err()
			}
			return handler(ctx, request)
		}),
		grpc.StreamInterceptor(func(srv any, stream grpc.ServerStream, _ *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
			seen.authorization <- metadata.ValueFromIncomingContext(stream.Context(), "authorization")
			return handler(srv, stream)
		}),
	)
	healthpb.RegisterHealthServer(server, health.NewServer())
	serverAddr := grpcserver.Serve(t, server)
	connection, err := Dial(serverAddr, tokenPath, timeout, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	return healthpb.NewHealthClient(connection), seen
}

func writeToken(t *testing.T, path, token string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(token), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestEachCallCarriesTheTokenFileAsItIsThen(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	writeToken(t, path, "test-token-one\n")
	client, seen := start(t, path, time.Minute)
	for _, want := range []string{"Bearer test-token-one", "Bearer test-token-two"} {
		if _, err := client.Check(t.Context(), &healthpb.HealthCheckRequest{}); err != nil {
			t.Fatal(err)
		}
		if got := <-seen.authorization; len(got) != 1 || got[0] != want {
			t.Errorf("authorization %q, want %q", got, want)
		}
		writeToken(t, path, "test-token-two")
	}
}

func TestStreamCarriesTheProjectedToken(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	writeToken(t, path, "test-stream-token\n")
	client, seen := start(t, path, time.Minute)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	if _, err := client.Watch(ctx, &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatal(err)
	}
	if got := <-seen.authorization; len(got) != 1 || got[0] != "Bearer test-stream-token" {
		t.Errorf("authorization %q, want the projected token", got)
	}
}

func TestAnUnreadableTokenFailsTheCallAsUnavailableBeforeItIsSent(t *testing.T) {
	for name, prepare := range map[string]func(t *testing.T, path string){
		"missing": func(*testing.T, string) {},
		"empty":   func(t *testing.T, path string) { writeToken(t, path, " \n") },
	} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "token")
			prepare(t, path)
			client, seen := start(t, path, time.Minute)
			_, err := client.Check(t.Context(), &healthpb.HealthCheckRequest{})
			if status.Code(err) != codes.Unavailable {
				t.Fatalf("code %v, want Unavailable: %v", status.Code(err), err)
			}
			if !IsTokenFileError(err) {
				t.Errorf("error %v is not identified as a token-file failure", err)
			}
			select {
			case <-seen.authorization:
				t.Error("a call without a token reached the server")
			default:
			}
		})
	}
}

func TestRemoteUnavailableCannotBeMistakenForATokenFileError(t *testing.T) {
	err := status.Error(codes.Unavailable, tokenFileEmptyError)
	if IsTokenFileError(err) {
		t.Errorf("remote error %v was identified as a local token-file error", err)
	}
}

func TestACallEndsWithDeadlineExceededOnceItsTimeoutPasses(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	writeToken(t, path, "test-token")
	client, _ := start(t, path, 50*time.Millisecond)
	_, err := client.Check(t.Context(), &healthpb.HealthCheckRequest{Service: blockedService})
	if status.Code(err) != codes.DeadlineExceeded {
		t.Fatalf("code %v, want DeadlineExceeded: %v", status.Code(err), err)
	}
}

func TestDialRefusesWhatEveryCallNeeds(t *testing.T) {
	for name, dial := range map[string]func() error{
		"no target":  func() error { _, err := Dial("", "/token", time.Second, nil); return err },
		"no token":   func() error { _, err := Dial("test-host:1", "", time.Second, nil); return err },
		"no timeout": func() error { _, err := Dial("test-host:1", "/token", 0, nil); return err },
	} {
		if dial() == nil {
			t.Errorf("%s: dialing succeeded", name)
		}
	}
}

func TestServiceSVIDCallsHaveNoBearerForUnaryAndStream(t *testing.T) {
	ca := peertlstesting.NewCA(t)
	serverConfig := ca.Issue(t, peertls.HistoryIngressURI)
	serverTLS, err := serverConfig.WorkloadServerTLSConfigWithServices(peertls.HistoryIngressURI, false)
	if err != nil {
		t.Fatal(err)
	}
	credentials, err := peertls.ServerCredentials(serverTLS)
	if err != nil {
		t.Fatal(err)
	}
	seen := make(chan metadata.MD, 2)
	inspect := func(ctx context.Context) {
		t.Helper()
		uri, err := peertls.PeerIdentity(ctx)
		if err != nil || uri != peertls.AppURI {
			t.Errorf("peer = %q, %v", uri, err)
		}
		md, _ := metadata.FromIncomingContext(ctx)
		seen <- md
	}
	server := grpc.NewServer(grpc.Creds(credentials),
		grpc.UnaryInterceptor(func(ctx context.Context, request any, _ *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
			inspect(ctx)
			return handler(ctx, request)
		}),
		grpc.StreamInterceptor(func(srv any, stream grpc.ServerStream, _ *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
			inspect(stream.Context())
			return handler(srv, stream)
		}))
	healthpb.RegisterHealthServer(server, health.NewServer())
	serverAddr := grpcserver.Serve(t, server)
	leaf := ca.Issue(t, peertls.AppURI)
	transport := &peertls.ClientTLS{
		CAFile: ca.RootFile, ServerURI: peertls.HistoryIngressURI,
		CertFile: leaf.CertFile, KeyFile: leaf.KeyFile, ClientURI: peertls.AppURI,
	}
	conn, err := Dial(serverAddr, "", time.Second, transport)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	client := healthpb.NewHealthClient(conn)
	if _, err := client.Check(t.Context(), &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	watch, err := client.Watch(ctx, &healthpb.HealthCheckRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := watch.Recv(); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if md := <-seen; len(md.Get("authorization")) != 0 {
			t.Fatalf("SVID call sent bearer: %v", md)
		}
	}
	if _, err := client.Check(metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer forbidden"), &healthpb.HealthCheckRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("mixed outgoing auth = %v", err)
	}
	if _, err := Dial(serverAddr, "/unused-token", time.Second, transport); err == nil {
		t.Fatal("accepted configured bearer plus SVID")
	}
}
