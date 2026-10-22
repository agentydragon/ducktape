package bearer

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

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
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go server.Serve(listener)
	t.Cleanup(server.Stop)
	connection, err := Dial(listener.Addr().String(), tokenPath, timeout)
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
		"no target":  func() error { _, err := Dial("", "/token", time.Second); return err },
		"no token":   func() error { _, err := Dial("test-host:1", "", time.Second); return err },
		"no timeout": func() error { _, err := Dial("test-host:1", "/token", 0); return err },
	} {
		if dial() == nil {
			t.Errorf("%s: dialing succeeded", name)
		}
	}
}
