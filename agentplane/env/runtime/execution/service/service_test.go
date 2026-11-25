package service

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/backend"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexshim"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/coder/websocket"
	"golang.org/x/net/http2"
	"golang.org/x/net/http2/h2c"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestSystemdExecutionRequiresReconciledRuntimeScope(t *testing.T) {
	config := Config{
		Namespace: "test-namespace", SandboxName: "test-sandbox", SandboxUID: "test-sandbox-uid",
		IncarnationUID: "test-incarnation", StateDir: "/state/execution",
		Binary: "/bin/codex", WorkingDirectory: "/state", ExecutorURL: "ws://127.0.0.1:4501", Owner: backend.OwnerSystemd, Placement: backend.PlacementDirect,
	}
	if err := RunEmbedded(t.Context(), config, func(executorpb.ExecutorServer, context.Context) { t.Error("unowned execution became ready") }); err == nil {
		t.Fatal("systemd execution accepted without Runtime scope and host")
	}
}

func TestExecutionConfigResolvesCompatibleReadinessDefaultsAndRejectsInvalidWindows(t *testing.T) {
	config := Config{
		Namespace: "test-namespace", SandboxName: "test-sandbox", SandboxUID: "test-sandbox-uid",
		IncarnationUID: "test-incarnation", StateDir: "/state/execution",
		ContainerSocket: "/run/executor.sock", ExecutorURL: "ws://127.0.0.1:4501", Owner: backend.OwnerCompanion, Placement: backend.PlacementDirect,
	}
	resolved, err := config.Resolve()
	if err != nil {
		t.Fatalf("resolve embedded config defaults: %v", err)
	}
	want := backend.Readiness{Timeout: 20 * time.Second, ProbeTimeout: 2 * time.Second, ProbeInterval: 100 * time.Millisecond}
	if resolved.Readiness != want {
		t.Fatalf("resolved readiness = %+v, want compatible defaults %+v", resolved.Readiness, want)
	}
	for _, test := range []struct {
		name      string
		readiness backend.Readiness
	}{
		{name: "timeout", readiness: backend.Readiness{Timeout: -time.Millisecond}},
		{name: "probe timeout", readiness: backend.Readiness{ProbeTimeout: -time.Millisecond}},
		{name: "probe interval", readiness: backend.Readiness{ProbeInterval: -time.Millisecond}},
	} {
		t.Run(test.name, func(t *testing.T) {
			invalid := config
			invalid.Readiness = test.readiness
			if _, err := invalid.Resolve(); err == nil {
				t.Fatal("resolved negative readiness duration")
			}
		})
	}
}

func TestRunReturnsFencedBackendLossOnlyAfterCleanupFence(t *testing.T) {
	for _, test := range []struct {
		name       string
		backendErr error
		fenceErr   error
		wantFenced bool
	}{
		{name: "owned companion loss", backendErr: errors.Join(backend.ErrExecutionBackendLost, backend.ErrContainerOwnershipLost, errors.New("EOF")), wantFenced: true},
		{name: "systemd native backend loss", backendErr: errors.Join(backend.ErrExecutionBackendLost, errors.New("native unit stopped")), wantFenced: true},
		{name: "generic backend loss", backendErr: errors.New("health probe failed")},
		{name: "companion fence failure", backendErr: errors.Join(backend.ErrContainerOwnershipLost, errors.New("EOF")), fenceErr: errors.New("fresh idle generation not observed")},
	} {
		t.Run(test.name, func(t *testing.T) {
			endpoint := testExecutorEndpoint(t)
			stateDir := t.TempDir()
			config := Config{
				Namespace: "test-namespace", SandboxName: "test-sandbox", SandboxUID: "test-sandbox-uid",
				IncarnationUID: "test-incarnation", StateDir: stateDir,
				ContainerSocket: filepath.Join(t.TempDir(), "companion.sock"), ExecutorURL: endpoint, Owner: backend.OwnerCompanion, Placement: backend.PlacementDirect,
			}
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			backendDone := make(chan error, 1)
			stopEntered, releaseStop := make(chan struct{}), make(chan struct{})
			stopResult := make(chan error, 1)
			ready := make(chan struct{}, 1)
			go func() {
				stopResult <- runWithReady(ctx, config, func(context.Context) (*supervision.Instance, error) {
					return &supervision.Instance{
						Done: backendDone,
						Stop: func(context.Context) error {
							close(stopEntered)
							<-releaseStop
							return test.fenceErr
						},
					}, nil
				}, func(executorpb.ExecutorServer, context.Context) { ready <- struct{}{} })
			}()
			select {
			case <-ready:
			case err := <-stopResult:
				t.Fatalf("service exited before readiness: %v", err)
			case <-time.After(5 * time.Second):
				t.Fatal("service did not publish readiness")
			}
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

func TestEmbeddedServicePublishesDirectAdapter(t *testing.T) {
	endpoint := testExecutorEndpoint(t)
	config := Config{
		Namespace: "test-namespace", SandboxName: "test-sandbox", SandboxUID: "test-sandbox-uid",
		IncarnationUID: "test-incarnation", StateDir: t.TempDir(),
		ExecutorURL: endpoint, Owner: backend.OwnerCompanion, Placement: backend.PlacementDirect, ContainerSocket: filepath.Join(t.TempDir(), "companion.sock"),
	}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	ready := make(chan executorpb.ExecutorServer, 1)
	finished := make(chan error, 1)
	go func() {
		finished <- runWithReady(ctx, config, func(context.Context) (*supervision.Instance, error) {
			return &supervision.Instance{Done: make(chan error), Stop: func(context.Context) error { return nil }}, nil
		}, func(executor executorpb.ExecutorServer, _ context.Context) { ready <- executor })
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

func TestEmbeddedBackendLossDuringReadinessKeepsFenceEvidence(t *testing.T) {
	attempted := make(chan struct{}, 1)
	server := testServer(t, http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		attempted <- struct{}{}
		connection, err := websocket.Accept(writer, request, nil)
		if err != nil {
			return
		}
		defer connection.CloseNow()
		_, _, _ = connection.Read(request.Context())
		<-request.Context().Done()
	}))
	t.Cleanup(server.Close)
	backendDone := make(chan error, 1)
	stopEntered := make(chan struct{})
	releaseStop := make(chan struct{})
	ready := make(chan struct{}, 1)
	finished := make(chan error, 1)
	go func() {
		finished <- runWithReady(t.Context(), Config{Implementation: backend.ImplementationCodex, ExecutorURL: "ws" + strings.TrimPrefix(server.URL, "http")}, func(context.Context) (*supervision.Instance, error) {
			return &supervision.Instance{Done: backendDone, Stop: func(context.Context) error {
				close(stopEntered)
				<-releaseStop
				return nil
			}}, nil
		}, func(executorpb.ExecutorServer, context.Context) { ready <- struct{}{} })
	}()
	select {
	case <-attempted:
	case err := <-finished:
		t.Fatalf("service exited before backend readiness probe: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("backend readiness probe did not start")
	}
	backendDone <- errors.Join(backend.ErrExecutionBackendLost, errors.New("native backend exited"))
	select {
	case <-stopEntered:
	case err := <-finished:
		t.Fatalf("service returned before backend fence: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("backend loss did not start native fence")
	}
	select {
	case <-ready:
		t.Fatal("service published an adapter after backend loss")
	case err := <-finished:
		t.Fatalf("service returned before backend fence: %v", err)
	default:
	}
	close(releaseStop)
	select {
	case err := <-finished:
		if !errors.Is(err, ErrFencedBackendLoss) {
			t.Fatalf("service error = %v, want fenced backend loss", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("service did not return after native fence")
	}
}

func TestConfiguredReadinessControlsEmbeddedBackendProbes(t *testing.T) {
	const (
		probeTimeout  = 70 * time.Millisecond
		probeInterval = 270 * time.Millisecond
	)
	var probes atomic.Int32
	probeTimes := make(chan time.Time, 8)
	firstProbeRead := make(chan struct{})
	releaseFirstProbe := make(chan struct{})
	var releaseFirst sync.Once
	t.Cleanup(func() { releaseFirst.Do(func() { close(releaseFirstProbe) }) })
	server := testServer(t, http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		probe := probes.Add(1)
		probeTimes <- time.Now()
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
			if probe == 1 {
				select {
				case <-firstProbeRead:
				default:
					close(firstProbeRead)
				}
				select {
				case <-releaseFirstProbe:
					return
				case <-request.Context().Done():
					return
				}
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
	endpoint := "ws" + strings.TrimPrefix(server.URL, "http")
	config := Config{
		ExecutorURL: endpoint, Implementation: backend.ImplementationCodex,
		Readiness: backend.Readiness{Timeout: 900 * time.Millisecond, ProbeTimeout: probeTimeout, ProbeInterval: probeInterval},
	}
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	ready := make(chan struct{}, 1)
	finished := make(chan error, 1)
	go func() {
		finished <- runWithReady(ctx, config, func(context.Context) (*supervision.Instance, error) {
			return &supervision.Instance{Done: make(chan error), Stop: func(context.Context) error { return nil }}, nil
		}, func(executorpb.ExecutorServer, context.Context) { ready <- struct{}{} })
	}()
	var first time.Time
	select {
	case first = <-probeTimes:
	case err := <-finished:
		t.Fatalf("service exited before first configured probe: %v", err)
	case <-time.After(time.Second):
		t.Fatal("backend probe did not start")
	}
	select {
	case <-firstProbeRead:
	case err := <-finished:
		t.Fatalf("service exited before the first probe blocked: %v", err)
	case <-time.After(time.Second):
		t.Fatal("first backend probe did not reach its response wait")
	}
	var second time.Time
	select {
	case second = <-probeTimes:
	case err := <-finished:
		t.Fatalf("service exited before retrying the backend probe: %v", err)
	case <-time.After(750 * time.Millisecond):
		t.Fatal("backend did not retry within its configured readiness window")
	}
	if delay := second.Sub(first); delay < 250*time.Millisecond || delay > 750*time.Millisecond {
		t.Fatalf("second backend probe started after %s; want configured probe timeout and interval", delay)
	}
	releaseFirst.Do(func() { close(releaseFirstProbe) })
	select {
	case <-ready:
	case err := <-finished:
		t.Fatalf("service exited before publishing configured readiness: %v", err)
	case <-time.After(time.Second):
		t.Fatal("service did not publish after the configured retry succeeded")
	}
	cancel()
	select {
	case err := <-finished:
		if err != nil {
			t.Fatalf("service shutdown: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("service did not join after cancellation")
	}
}

func TestConfiguredReadinessDeadlineAndCancellationFenceBackend(t *testing.T) {
	t.Run("deadline", func(t *testing.T) {
		server := testServer(t, http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
			writer.WriteHeader(http.StatusServiceUnavailable)
		}))
		t.Cleanup(server.Close)
		endpoint := "ws" + strings.TrimPrefix(server.URL, "http")
		config := Config{
			ExecutorURL: endpoint, Implementation: backend.ImplementationCodex,
			Readiness: backend.Readiness{Timeout: 150 * time.Millisecond, ProbeTimeout: 80 * time.Millisecond, ProbeInterval: 20 * time.Millisecond},
		}
		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()
		ready := make(chan struct{}, 1)
		stopped := make(chan struct{})
		finished := make(chan error, 1)
		started := time.Now()
		go func() {
			finished <- runWithReady(ctx, config, func(context.Context) (*supervision.Instance, error) {
				return &supervision.Instance{Done: make(chan error), Stop: func(context.Context) error { close(stopped); return nil }}, nil
			}, func(executorpb.ExecutorServer, context.Context) { ready <- struct{}{} })
		}()
		select {
		case err := <-finished:
			if !errors.Is(err, context.DeadlineExceeded) {
				t.Fatalf("backend readiness error = %v, want configured deadline", err)
			}
		case <-time.After(time.Second):
			t.Fatal("backend readiness did not honor the configured deadline")
		}
		if elapsed := time.Since(started); elapsed < 120*time.Millisecond || elapsed > 500*time.Millisecond {
			t.Fatalf("backend readiness failed after %s, want about 150ms", elapsed)
		}
		if ctx.Err() != nil {
			t.Fatalf("owner context canceled by independent readiness deadline: %v", ctx.Err())
		}
		select {
		case <-stopped:
		default:
			t.Fatal("backend was not stopped after readiness timeout")
		}
		select {
		case <-ready:
			t.Fatal("adapter published after backend readiness timeout")
		default:
		}
	})

	t.Run("caller cancellation", func(t *testing.T) {
		attempted := make(chan struct{}, 1)
		server := testServer(t, http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
			attempted <- struct{}{}
			connection, err := websocket.Accept(writer, request, nil)
			if err != nil {
				return
			}
			defer connection.CloseNow()
			_, _, _ = connection.Read(request.Context())
			<-request.Context().Done()
		}))
		t.Cleanup(server.Close)
		endpoint := "ws" + strings.TrimPrefix(server.URL, "http")
		config := Config{
			ExecutorURL: endpoint, Implementation: backend.ImplementationCodex,
			Readiness: backend.Readiness{Timeout: 5 * time.Second, ProbeTimeout: 4 * time.Second, ProbeInterval: 1 * time.Second},
		}
		ctx, cancel := context.WithCancel(t.Context())
		ready := make(chan struct{}, 1)
		stopped := make(chan struct{})
		cleanupErr := errors.New("backend fence failed")
		finished := make(chan error, 1)
		go func() {
			finished <- runWithReady(ctx, config, func(context.Context) (*supervision.Instance, error) {
				return &supervision.Instance{Done: make(chan error), Stop: func(context.Context) error { close(stopped); return cleanupErr }}, nil
			}, func(executorpb.ExecutorServer, context.Context) { ready <- struct{}{} })
		}()
		select {
		case <-attempted:
		case err := <-finished:
			t.Fatalf("service exited before backend probe: %v", err)
		case <-time.After(time.Second):
			t.Fatal("backend probe did not start")
		}
		cancel()
		select {
		case err := <-finished:
			if !errors.Is(err, cleanupErr) || errors.Is(err, context.Canceled) {
				t.Fatalf("canceled service error = %v, want only the failed backend fence", err)
			}
		case <-time.After(time.Second):
			t.Fatal("service did not join after caller cancellation")
		}
		select {
		case <-stopped:
		default:
			t.Fatal("backend fence did not complete before service returned")
		}
		select {
		case <-ready:
			t.Fatal("adapter published after caller cancellation")
		default:
		}
	})
}

func TestNativeHealthChecksDoNotConsumeExecutionSessions(t *testing.T) {
	limits := native.DefaultLimits()
	limits.MaxSessions = 1
	core, err := native.NewWithLimits(limits)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = core.Close() })

	checks := make(chan struct{}, 4)
	rpc := grpc.NewServer(grpc.UnaryInterceptor(func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		response, err := handler(ctx, req)
		if info.FullMethod == healthpb.Health_Check_FullMethodName {
			checks <- struct{}{}
		}
		return response, err
	}))
	h := health.NewServer()
	h.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(rpc, h)
	t.Cleanup(rpc.Stop)
	shim := codexshim.Handler{Server: core}
	server := httptest.NewServer(h2c.NewHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ProtoMajor == 2 && strings.HasPrefix(r.Header.Get("Content-Type"), "application/grpc") {
			rpc.ServeHTTP(w, r)
			return
		}
		shim.ServeHTTP(w, r)
	}), &http2.Server{}))
	t.Cleanup(server.Close)

	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	ready := make(chan struct{}, 1)
	finished := make(chan error, 1)
	go func() {
		finished <- runWithReady(ctx, Config{Implementation: backend.ImplementationNative, ExecutorURL: "ws" + strings.TrimPrefix(server.URL, "http")}, func(context.Context) (*supervision.Instance, error) {
			return &supervision.Instance{Done: make(chan error), Stop: func(context.Context) error { return nil }}, nil
		}, func(executorpb.ExecutorServer, context.Context) { ready <- struct{}{} })
	}()
	select {
	case <-ready:
	case err := <-finished:
		t.Fatalf("native service exited before readiness: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("native service did not become ready")
	}
	// Wait for both startup readiness and the first periodic liveness check.
	for i := 0; i < 2; i++ {
		select {
		case <-checks:
		case err := <-finished:
			t.Fatalf("native service exited during health checks: %v", err)
		case <-time.After(8 * time.Second):
			t.Fatal("native health check did not arrive")
		}
	}
	session, err := core.OpenSession("")
	if err != nil {
		t.Fatalf("health checks consumed the user session capacity: %v", err)
	}
	core.Detach(session.ID())
	cancel()
	select {
	case err := <-finished:
		if err != nil {
			t.Fatalf("native service shutdown: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("native service did not shut down")
	}
}

func TestTypedReadinessIsRequiredBeforePublishing(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer c.CloseNow()
		for {
			_, payload, err := c.Read(r.Context())
			if err != nil {
				return
			}
			response := ""
			switch {
			case strings.Contains(string(payload), `"id":1`):
				response = `{"id":1,"result":{"sessionId":"health"}}`
			case strings.Contains(string(payload), `"id":2`):
				response = `{"id":2,"result":{"status":"ready"}}`
			}
			if response != "" {
				if c.Write(r.Context(), websocket.MessageText, []byte(response)) != nil {
					return
				}
			}
		}
	}))
	defer server.Close()
	cfg := Config{ExecutorURL: "ws" + strings.TrimPrefix(server.URL, "http"), Readiness: backend.Readiness{Timeout: 200 * time.Millisecond, ProbeTimeout: 100 * time.Millisecond, ProbeInterval: 10 * time.Millisecond}}
	ready := false
	err := runWithReady(t.Context(), cfg, func(context.Context) (*supervision.Instance, error) {
		return &supervision.Instance{Done: make(chan error), Stop: func(context.Context) error { return nil }}, nil
	}, func(executorpb.ExecutorServer, context.Context) { ready = true })
	if ready || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("ready=%v, err=%v; want typed health timeout", ready, err)
	}
}

func testServer(t *testing.T, ws http.Handler) *httptest.Server {
	t.Helper()
	rpc := grpc.NewServer()
	h := health.NewServer()
	h.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(rpc, h)
	t.Cleanup(rpc.Stop)
	return httptest.NewServer(h2c.NewHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ProtoMajor == 2 && strings.HasPrefix(r.Header.Get("Content-Type"), "application/grpc") {
			rpc.ServeHTTP(w, r)
			return
		}
		ws.ServeHTTP(w, r)
	}), &http2.Server{}))
}

func testExecutorEndpoint(t *testing.T) string {
	t.Helper()
	server := testServer(t, http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
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

func TestAdmissionRejectsForeignOrStaleScopeBeforeEffect(t *testing.T) {
	config := Config{Namespace: "test-namespace", SandboxName: "test-sandbox", SandboxUID: "test-sandbox-uid", IncarnationUID: "test-incarnation"}
	scope := &executorpb.ExecutionScope{
		Destination: &identitypb.SandboxDestination{
			Owner:  &identitypb.ServiceAccount{Namespace: config.Namespace, Name: config.SandboxName},
			Target: &identitypb.EnvironmentTarget{Namespace: config.Namespace, Name: config.SandboxName, Uid: config.SandboxUID},
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
		{"missing-target", func(s *executorpb.ExecutionScope) { s.Destination.Target = nil }, codes.PermissionDenied},
		{"sandbox", func(s *executorpb.ExecutionScope) { s.Destination.Target.Name = "another-sandbox" }, codes.PermissionDenied},
		{"uid", func(s *executorpb.ExecutionScope) { s.Destination.Target.Uid = "another-uid" }, codes.PermissionDenied},
		{"incarnation", func(s *executorpb.ExecutionScope) { s.ExpectedIncarnationUid = "old-incarnation" }, codes.FailedPrecondition},
		{"session", func(s *executorpb.ExecutionScope) { s.SessionId = "" }, codes.InvalidArgument},
		{"session-too-long", func(s *executorpb.ExecutionScope) { s.SessionId = strings.Repeat("x", 129) }, codes.InvalidArgument},
	} {
		t.Run(test.name, func(t *testing.T) {
			candidate := proto.Clone(scope).(*executorpb.ExecutionScope)
			test.change(candidate)
			accepted := false
			err := config.admit(t.Context(), candidate, true, func(context.Context) error { accepted = true; return nil })
			if status.Code(err) != test.code || accepted != (test.code == codes.OK) {
				t.Fatalf("admission: accepted=%v error=%v, want %s", accepted, err, test.code)
			}
			if test.code == codes.FailedPrecondition && len(status.Convert(err).Details()) != 0 {
				t.Fatalf("embedded admission carried status details: %v", status.Convert(err).Details())
			}
		})
	}
}
