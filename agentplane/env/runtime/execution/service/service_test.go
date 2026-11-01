package service

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/coder/websocket"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestOwnerLockFencesContenderAndSurvivesSocketReplacement(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "service")
	owner, err := lock(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer owner.Close()
	if err := os.WriteFile(filepath.Join(directory, "execution.sock"), []byte("stale socket"), 0600); err != nil {
		t.Fatal(err)
	}
	if contender, err := lock(directory); err == nil {
		_ = contender.Close()
		t.Fatal("second service acquired the live execution owner lock")
	}
	if err := owner.Close(); err != nil {
		t.Fatal(err)
	}
	successor, err := lock(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer successor.Close()
}

func TestOwnerLockRestoresPrivateModeAfterVolumeRemount(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "service")
	if err := os.MkdirAll(directory, 0700); err != nil {
		t.Fatal(err)
	}
	// fsGroup may add group permissions and setgid to a persistent directory
	// when Kubernetes mounts it into a replacement Pod.
	if err := os.Chmod(directory, 02770); err != nil {
		t.Fatal(err)
	}

	owner, err := lock(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer owner.Close()

	info, err := os.Stat(directory)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0700 || info.Mode()&os.ModeSetgid != 0 {
		t.Fatalf("execution state directory mode = %v, want 0700", info.Mode())
	}
}

func TestOwnerLockRejectsDirectoryAndOwnerSymlinks(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "target")
	if err := os.Mkdir(target, 0700); err != nil {
		t.Fatal(err)
	}
	directoryLink := filepath.Join(root, "service-link")
	if err := os.Symlink(target, directoryLink); err != nil {
		t.Fatal(err)
	}
	if owner, err := lock(directoryLink); err == nil {
		_ = owner.Close()
		t.Fatal("owner lock followed a symlink for the state directory")
	}

	ownerLinkDir := filepath.Join(root, "service")
	if err := os.Mkdir(ownerLinkDir, 0700); err != nil {
		t.Fatal(err)
	}
	ownerTarget := filepath.Join(root, "outside-owner")
	if err := os.WriteFile(ownerTarget, []byte("outside"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(ownerTarget, filepath.Join(ownerLinkDir, ".agentplane-execution-owner")); err != nil {
		t.Fatal(err)
	}
	if owner, err := lock(ownerLinkDir); err == nil {
		_ = owner.Close()
		t.Fatal("owner lock followed a symlink for the lock file")
	}
}

func TestSocketPathCanBeShortenedIndependentlyOfOwnerState(t *testing.T) {
	config := Config{
		Namespace: "test-namespace", SandboxName: "test-sandbox", SandboxUID: "test-sandbox-uid",
		IncarnationUID: "test-incarnation", StateDir: "/state/execution", SocketPath: "/state/e.sock",
		ContainerSocket: "/run/agentplane/executor.sock", ExecutorURL: "ws://127.0.0.1:4501", Backend: "companion",
	}
	if err := config.Validate(); err != nil {
		t.Fatal(err)
	}
	if got := config.Socket(); got != config.SocketPath {
		t.Fatalf("Socket() = %q, want %q", got, config.SocketPath)
	}

	config.SocketPath = "relative.sock"
	if err := config.Validate(); err == nil {
		t.Fatal("Validate accepted a relative socket path")
	}
}

func TestSystemdExecutionRequiresExplicitOwnerLifetime(t *testing.T) {
	config := Config{
		Namespace: "test-namespace", SandboxName: "test-sandbox", SandboxUID: "test-sandbox-uid",
		IncarnationUID: "test-incarnation", StateDir: "/state/execution", SocketPath: "/state/e.sock",
		Binary: "/bin/codex", WorkingDirectory: "/state", ExecutorURL: "ws://127.0.0.1:4501",
		Backend: "systemd", Systemd: &SystemdConfig{},
	}
	if err := config.Validate(); err == nil || !strings.Contains(err.Error(), "explicit owner unit") {
		t.Fatalf("missing systemd owner = %v, want explicit owner rejection", err)
	}
	config.Systemd.OwnerUnit = "agentplane-environment-runtime.service"
	config.Systemd.LifetimeDirectory = "/run/agentplane-environment-runtime-lifetime"
	if err := config.Validate(); err != nil {
		t.Fatalf("runtime-owned systemd execution config = %v", err)
	}
}

func TestRunReturnsFencedBackendLossOnlyAfterCleanupFence(t *testing.T) {
	for _, test := range []struct {
		name       string
		backendErr error
		fenceErr   error
		wantFenced bool
	}{
		{name: "owned companion loss", backendErr: errors.Join(codexexecserver.ErrExecutionBackendLost, codexexecserver.ErrContainerOwnershipLost, errors.New("EOF")), wantFenced: true},
		{name: "systemd native backend loss", backendErr: errors.Join(codexexecserver.ErrExecutionBackendLost, errors.New("native unit stopped")), wantFenced: true},
		{name: "generic backend loss", backendErr: errors.New("health probe failed")},
		{name: "companion fence failure", backendErr: errors.Join(codexexecserver.ErrContainerOwnershipLost, errors.New("EOF")), fenceErr: errors.New("fresh idle generation not observed")},
	} {
		t.Run(test.name, func(t *testing.T) {
			endpoint := testExecutorEndpoint(t)
			stateDir := t.TempDir()
			socketPath := filepath.Join(t.TempDir(), "e.sock")
			config := Config{
				Namespace: "test-namespace", SandboxName: "test-sandbox", SandboxUID: "test-sandbox-uid",
				IncarnationUID: "test-incarnation", StateDir: stateDir, SocketPath: socketPath,
				ContainerSocket: filepath.Join(t.TempDir(), "companion.sock"), ExecutorURL: endpoint, Backend: "companion",
			}
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			backendDone := make(chan error, 1)
			stopEntered, releaseStop := make(chan struct{}), make(chan struct{})
			stopResult := make(chan error, 1)
			go func() {
				stopResult <- run(ctx, config, func(context.Context) (*supervision.Instance, error) {
					return &supervision.Instance{
						Done: backendDone,
						Stop: func(context.Context) error {
							close(stopEntered)
							<-releaseStop
							return test.fenceErr
						},
					}, nil
				})
			}()
			serviceConn := waitTestService(t, ctx, socketPath)
			defer serviceConn.Close()
			backendDone <- test.backendErr
			select {
			case <-stopEntered:
			case <-time.After(5 * time.Second):
				t.Fatal("Execution service did not stop and fence its backend")
			}
			select {
			case err := <-stopResult:
				t.Fatalf("Run returned before backend cleanup fence completed: %v", err)
			default:
			}
			close(releaseStop)
			select {
			case err := <-stopResult:
				if fenced := errors.Is(err, ErrFencedBackendLoss); fenced != test.wantFenced {
					t.Fatalf("Run error = %v, fenced marker = %v; want %v", err, fenced, test.wantFenced)
				}
				if test.fenceErr != nil && !errors.Is(err, test.fenceErr) {
					t.Fatalf("Run error = %v, want fence error %v", err, test.fenceErr)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("Run did not finish after backend cleanup fence completed")
			}
		})
	}
}

func TestEmbeddedServicePublishesDirectAdapterAndHealthOnlySocket(t *testing.T) {
	endpoint := testExecutorEndpoint(t)
	config := Config{
		Namespace: "test-namespace", SandboxName: "test-sandbox", SandboxUID: "test-sandbox-uid",
		IncarnationUID: "test-incarnation", StateDir: t.TempDir(), SocketPath: filepath.Join(t.TempDir(), "e.sock"),
		ExecutorURL: endpoint, Backend: "companion", ContainerSocket: filepath.Join(t.TempDir(), "companion.sock"),
	}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	ready := make(chan executorpb.ExecutorServer, 1)
	finished := make(chan error, 1)
	go func() {
		finished <- runWithReady(ctx, config, func(context.Context) (*supervision.Instance, error) {
			return &supervision.Instance{Done: make(chan error), Stop: func(context.Context) error { return nil }}, nil
		}, func(executor executorpb.ExecutorServer) { ready <- executor })
	}()
	select {
	case executor := <-ready:
		if executor == nil {
			t.Fatal("embedded service published a nil adapter")
		}
	case err := <-finished:
		t.Fatalf("embedded service exited before readiness: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("embedded service did not publish readiness")
	}
	conn, err := grpc.NewClient("unix://"+config.Socket(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	checkCtx, finishCheck := context.WithTimeout(ctx, 5*time.Second)
	defer finishCheck()
	health, err := healthpb.NewHealthClient(conn).Check(checkCtx, &healthpb.HealthCheckRequest{})
	if err != nil || health.GetStatus() != healthpb.HealthCheckResponse_SERVING {
		t.Fatalf("embedded health = %v, err=%v", health, err)
	}
	_, err = executorpb.NewExecutorClient(conn).StartProcess(checkCtx, &executorpb.StartProcessRequest{})
	if status.Code(err) != codes.Unimplemented {
		t.Fatalf("private Executor RPC = %v, want Unimplemented", err)
	}
	cancel()
	select {
	case err := <-finished:
		if err != nil {
			t.Fatalf("embedded service shutdown: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("embedded service did not join after shutdown")
	}
}

func testExecutorEndpoint(t *testing.T) string {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		connection, err := websocket.Accept(writer, request, nil)
		if err != nil {
			return
		}
		defer connection.CloseNow()
		for {
			_, payload, err := connection.Read(request.Context())
			if err != nil {
				return
			}
			response := ""
			switch {
			case strings.Contains(string(payload), `"id":1`):
				response = `{"id":1,"result":{"sessionId":"health-session"}}`
			case strings.Contains(string(payload), `"id":2`):
				response = `{"id":2,"result":{"status":"ready"}}`
			}
			if response != "" && connection.Write(request.Context(), websocket.MessageText, []byte(response)) != nil {
				return
			}
		}
	}))
	t.Cleanup(server.Close)
	return "ws" + strings.TrimPrefix(server.URL, "http")
}

func waitTestService(t *testing.T, ctx context.Context, socketPath string) *grpc.ClientConn {
	t.Helper()
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	deadline, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	var connection *grpc.ClientConn
	for {
		if connection == nil {
			var err error
			connection, err = grpc.NewClient("unix://"+socketPath, grpc.WithTransportCredentials(insecure.NewCredentials()))
			if err != nil {
				select {
				case <-deadline.Done():
					t.Fatalf("dial Execution service: %v", err)
				case <-ticker.C:
					continue
				}
			}
		}
		checkCtx, checkCancel := context.WithTimeout(deadline, 100*time.Millisecond)
		response, err := healthpb.NewHealthClient(connection).Check(checkCtx, &healthpb.HealthCheckRequest{Service: executorpb.Executor_ServiceDesc.ServiceName})
		checkCancel()
		if err == nil && response.Status == healthpb.HealthCheckResponse_SERVING {
			return connection
		}
		select {
		case <-deadline.Done():
			_ = connection.Close()
			t.Fatalf("Execution service did not become ready: %v", err)
		case <-ticker.C:
		}
	}
}

func TestAdmissionRejectsForeignOrStaleScopeBeforeEffect(t *testing.T) {
	config := Config{Namespace: "test-namespace", SandboxName: "test-sandbox", SandboxUID: "test-sandbox-uid", IncarnationUID: "test-incarnation"}
	scope := &executorpb.ExecutionScope{
		Destination: &sandboxespb.SandboxDestination{
			Owner:   &sandboxespb.ServiceAccount{Namespace: config.Namespace, Name: config.SandboxName},
			Sandbox: config.SandboxName, SandboxUid: config.SandboxUID,
		}, ExpectedIncarnationUid: config.IncarnationUID, SessionId: "test-session",
	}
	for _, test := range []struct {
		name   string
		change func(*executorpb.ExecutionScope)
		code   codes.Code
	}{
		{"current", func(*executorpb.ExecutionScope) {}, codes.OK},
		{"namespace", func(s *executorpb.ExecutionScope) { s.Destination.Owner.Namespace = "another-namespace" }, codes.PermissionDenied},
		{"owner", func(s *executorpb.ExecutionScope) { s.Destination.Owner.Name = "another-owner" }, codes.PermissionDenied},
		{"sandbox", func(s *executorpb.ExecutionScope) { s.Destination.Sandbox = "another-sandbox" }, codes.PermissionDenied},
		{"uid", func(s *executorpb.ExecutionScope) { s.Destination.SandboxUid = "another-uid" }, codes.PermissionDenied},
		{"incarnation", func(s *executorpb.ExecutionScope) { s.ExpectedIncarnationUid = "old-incarnation" }, codes.FailedPrecondition},
		{"session", func(s *executorpb.ExecutionScope) { s.SessionId = "" }, codes.InvalidArgument},
	} {
		t.Run(test.name, func(t *testing.T) {
			candidate := proto.Clone(scope).(*executorpb.ExecutionScope)
			test.change(candidate)
			accepted := false
			err := config.admit(t.Context(), candidate, true, func(context.Context) error { accepted = true; return nil })
			if status.Code(err) != test.code || accepted != (test.code == codes.OK) {
				t.Fatalf("admission: accepted=%v error=%v, want %s", accepted, err, test.code)
			}
		})
	}
}
