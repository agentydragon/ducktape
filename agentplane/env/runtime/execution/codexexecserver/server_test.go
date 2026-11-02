package codexexecserver

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	executorcontrol "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/companion/control"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/coder/websocket"
	"github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"
)

func TestMain(m *testing.M) {
	if len(os.Args) > 1 && os.Args[1] == supervision.SupervisorMode {
		os.Exit(supervision.SupervisorMain(os.Args[2:]))
	}
	os.Exit(m.Run())
}

func TestNativeStartUsesManagedProcessAdmission(t *testing.T) {
	wantErr := errors.New("managed admission refused")
	var observed supervision.ProcessConfig
	start := NativeStart(NativeConfig{
		Config: Config{URL: "ws://127.0.0.1:4501"}, Binary: "/bin/codex", StateDir: "/state",
		Environment: map[string]string{"PATH": "/bin", "OPENAI_API_KEY": "secret"},
		LaunchProcess: func(_ context.Context, config supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			observed = config
			return nil, wantErr
		},
	})
	if _, err := start(context.Background()); !errors.Is(err, wantErr) {
		t.Fatalf("managed start error = %v, want %v", err, wantErr)
	}
	if observed.StopGrace != executorStopGrace || observed.FenceTimeout != fenceDeadline || observed.Dir != "/state" || strings.Join(observed.Command, " ") != "/bin/codex exec-server --listen ws://127.0.0.1:4501" {
		t.Fatalf("managed process configuration = %+v", observed)
	}
	if _, found := observed.Env["OPENAI_API_KEY"]; found || observed.Env["PATH"] != "/bin" {
		t.Fatal("managed executor received an unexpected environment")
	}
}

func TestNativeStartIsOwnedAndFencedByProcessController(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	engine, err := runtime.NewRuntime(store, zap.L().Named("executor-process-test"))
	if err != nil {
		t.Fatal(err)
	}
	var workers sync.WaitGroup
	const namespace resource.Namespace = "executor-test"
	host := supervision.NewHost(ctx, store, namespace, supervision.RuntimeDomain, &workers, func(err error) { t.Errorf("process host: %v", err) })
	if err := engine.RegisterQController(host.Controller()); err != nil {
		t.Fatal(err)
	}
	engineDone := make(chan error, 1)
	go func() { engineDone <- engine.Run(ctx) }()
	defer func() {
		cancel()
		if err := <-engineDone; err != nil && !errors.Is(err, context.Canceled) {
			t.Errorf("process engine: %v", err)
		}
		workers.Wait()
	}()
	binary := filepath.Join(t.TempDir(), "executor")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\nexec /bin/sleep 60\n"), 0700); err != nil {
		t.Fatal(err)
	}
	supervisor := supervision.NewSubprocessSupervisor(nil)
	start := NativeStart(NativeConfig{
		Config: Config{URL: "ws://127.0.0.1:4501"}, Binary: binary, StateDir: t.TempDir(),
		LaunchProcess: func(ctx context.Context, config supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return host.Launch(ctx, "execution-service", 42, supervisor, config)
		},
	})
	instance, err := start(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = instance.Stop(context.Background()) }()
	items, err := store.List(ctx, resource.NewMetadata(namespace, supervision.DesiredStateType, "", resource.VersionUndefined))
	if err != nil || len(items.Items) != 1 {
		t.Fatalf("executor desired states = %v, error %v", items, err)
	}
	id := items.Items[0].Metadata().ID()
	desired, found, err := supervision.ReadDesiredState(ctx, store, namespace, id)
	if err != nil || !found || desired.Owner != "execution-service" || desired.Generation != 42 || desired.StopTimeout != fenceDeadline {
		t.Fatalf("executor desired state = %+v, found=%t, error=%v", desired, found, err)
	}
	status, found, err := supervision.ReadStatus(ctx, store, namespace, id)
	if err != nil || !found || status.Phase != supervision.Running || status.PID != instance.PID {
		t.Fatalf("executor process status = %+v, found=%t, error=%v", status, found, err)
	}
	if err := instance.Stop(ctx); err != nil {
		t.Fatalf("fence managed executor: %v", err)
	}
	if _, found, err := supervision.ReadDesiredState(ctx, store, namespace, id); err != nil || found {
		t.Fatalf("executor intent after fence: found=%t error=%v", found, err)
	}
}

func TestProbeRequiresValidRPCAndBoundsUnresponsiveServer(t *testing.T) {
	for _, test := range []struct {
		name, response, status, wantError string
	}{
		{"healthy", `{"id":1,"result":{"sessionId":"test-session"}}`, `{"id":2,"result":{"status":"ready"}}`, ""},
		{"RPC error", `{"id":1,"error":{"code":-32603,"message":"unavailable"}}`, "", "executor health initialize returned no session"},
		{"wrong request", `{"id":2,"result":{"sessionId":"test-session"}}`, "", "executor health initialize returned no session"},
		{"stalled dispatcher", "", "", "executor initialize read"},
		{"invalid initialize JSON", "{", "", "executor initialize decode"},
		{"stalled status", `{"id":1,"result":{"sessionId":"test-session"}}`, "", "executor environment/status read"},
		{"invalid status JSON", `{"id":1,"result":{"sessionId":"test-session"}}`, "{", "executor environment/status decode"},
		{"not ready", `{"id":1,"result":{"sessionId":"test-session"}}`, `{"id":2,"result":{"status":"starting"}}`, `executor environment/status returned id=2 status="starting"`},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				connection, err := websocket.Accept(w, r, nil)
				if err != nil {
					return
				}
				defer connection.CloseNow()
				ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
				defer cancel()
				if _, _, err := connection.Read(ctx); err != nil {
					return
				}
				if test.response != "" {
					_ = connection.Write(ctx, websocket.MessageText, []byte(test.response))
				}
				if _, _, err := connection.Read(ctx); err != nil {
					return
				}
				if _, _, err := connection.Read(ctx); err != nil {
					return
				}
				if test.status != "" {
					_ = connection.Write(ctx, websocket.MessageText, []byte(test.status))
				}
				_, _, _ = connection.Read(ctx)
			}))
			defer server.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
			defer cancel()
			err := ProbeAt("ws" + strings.TrimPrefix(server.URL, "http"))(ctx)
			if test.wantError == "" && err != nil {
				t.Fatalf("probe = %v, want success", err)
			}
			if test.wantError != "" && (err == nil || !strings.Contains(err.Error(), test.wantError)) {
				t.Fatalf("probe = %v, want error containing %q", err, test.wantError)
			}
		})
	}
}

func TestFenceContainerBindsDelayedInspectToObservedGeneration(t *testing.T) {
	socket := filepath.Join(t.TempDir(), "companion.sock")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	fenceRequest := make(chan executorcontrol.Request, 1)
	served := make(chan error, 1)
	go func() {
		first, err := listener.Accept()
		if err != nil {
			served <- err
			return
		}
		_ = first.SetDeadline(time.Now().Add(3 * time.Second))
		var inspect executorcontrol.Request
		if err := json.NewDecoder(first).Decode(&inspect); err != nil || inspect.Operation != "inspect" {
			served <- fmt.Errorf("initial inspect = %+v, %v", inspect, err)
			_ = first.Close()
			return
		}
		if err := json.NewEncoder(first).Encode(executorcontrol.Status{Generation: "g1", Owned: true}); err != nil {
			served <- err
			_ = first.Close()
			return
		}
		// G1 is fenced and G2 is claimed before the delayed inspect response is acted on.
		var fence executorcontrol.Request
		if err := json.NewDecoder(first).Decode(&fence); err != nil {
			served <- err
			_ = first.Close()
			return
		}
		fenceRequest <- fence
		_ = first.Close()
		second, err := listener.Accept()
		if err != nil {
			served <- err
			return
		}
		defer second.Close()
		_ = second.SetDeadline(time.Now().Add(3 * time.Second))
		if err := json.NewDecoder(second).Decode(&inspect); err != nil || inspect.Operation != "inspect" {
			served <- fmt.Errorf("successor inspect = %+v, %v", inspect, err)
			return
		}
		if err := json.NewEncoder(second).Encode(executorcontrol.Status{Generation: "g2", Owned: true}); err != nil {
			served <- err
			return
		}
		var extra executorcontrol.Request
		if err := json.NewDecoder(second).Decode(&extra); err != io.EOF {
			served <- fmt.Errorf("unexpected request for successor: %+v, %v", extra, err)
			return
		}
		served <- nil
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := FenceContainer(ctx, socket, "g1"); err != nil {
		t.Fatal(err)
	}
	if request := <-fenceRequest; request.Operation != "fence" || request.Generation != "g1" {
		t.Fatalf("delayed fence request = %+v, want g1", request)
	}
	if err := <-served; err != nil {
		t.Fatal(err)
	}
}

func TestConfigurationRejectsPartialOrOverlappingLaunchers(t *testing.T) {
	for _, endpoint := range []string{"", "ws://0.0.0.0:4501", "ws://127.0.0.1:0", "wss://127.0.0.1:4501", "ws://127.0.0.1:4501/path", "ws://user@127.0.0.1:4501"} {
		if err := (Config{URL: endpoint}).Validate(nil); err == nil {
			t.Fatalf("accepted %q", endpoint)
		}
	}
	const endpoint = "ws://127.0.0.1:4501"
	if err := (Config{URL: endpoint, ContainerSocket: "/run/test-executor.sock"}).Validate(nil); err != nil {
		t.Fatal(err)
	}
	limits := supervision.ResourceLimits{CPUQuotaMicros: 100000, MemoryMaxBytes: 64 * 1024 * 1024, PidsMax: 32, ReadBPS: 1, WriteBPS: 1, ReadIOPS: 1, WriteIOPS: 1}
	harnessLauncher := &supervision.SystemdLauncher{Slice: supervision.WorkloadSlice, AggregateLimits: limits, ProcessLimits: limits}
	executorLauncher := *harnessLauncher
	executorLauncher.Slice = supervision.ExecutorSlice
	if err := (Config{URL: endpoint, Launcher: &executorLauncher}).Validate(harnessLauncher); err != nil {
		t.Fatal(err)
	}
	invalidLauncher := executorLauncher
	invalidLauncher.ProcessLimits.MemoryMaxBytes++
	for _, config := range []Config{
		{URL: endpoint},
		{URL: endpoint, ContainerSocket: "relative"},
		{URL: endpoint, ContainerSocket: "/run/test.sock", Launcher: harnessLauncher},
		{URL: endpoint, Launcher: harnessLauncher},
		{URL: endpoint, Launcher: &supervision.SystemdLauncher{Slice: "invalid.slice"}},
		{URL: endpoint, Launcher: &invalidLauncher},
	} {
		if err := config.Validate(harnessLauncher); err == nil {
			t.Fatalf("accepted %+v", config)
		}
	}
}
