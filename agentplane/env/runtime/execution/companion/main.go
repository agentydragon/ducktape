package executor

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/companion/control"
	controlpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/companion/control/controlpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/google/uuid"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
)

// Main must be PID 1 in a private container PID namespace.
// Returning to main exits that namespace, letting the runtime kill even children
// that escaped their process group. This mode never runs as an ordinary VM child.
func Main(args []string) int {
	flags := flag.NewFlagSet("agentplane-execution-service companion", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	socket := flags.String("socket", "", "local ownership socket")
	listen := flags.String("listen", "", "Codex WebSocket endpoint")
	binary := flags.String("binary", "", "Codex executable")
	healthListen := flags.String("health-listen", "", "HTTP health endpoint")
	if err := flags.Parse(args); err != nil {
		return 64
	}
	if os.Getpid() != 1 || !filepath.IsAbs(*socket) || !filepath.IsAbs(*binary) || *healthListen == "" || flags.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "executor companion requires PID 1, absolute socket and binary paths, an HTTP health address, and a private PID namespace")
		return 64
	}
	if err := validateURL(*listen); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 64
	}
	supervisor, err := supervision.NewPID1Supervisor()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	if err := serve(ctx, *socket, *binary, *listen, *healthListen, supervisor); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}

func validateURL(value string) error {
	u, err := url.Parse(value)
	if err != nil {
		return err
	}
	port, err := strconv.Atoi(u.Port())
	if err != nil || port < 1 || port > 65535 || u.Scheme != "ws" || u.Hostname() != "127.0.0.1" || u.User != nil || u.Path != "" || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" {
		return errors.New("executor URL must be ws://127.0.0.1:<nonzero port>")
	}
	return nil
}

func healthHandler(socket string) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), time.Second)
		defer cancel()
		connection, err := control.Dial(socket)
		if err != nil {
			http.Error(w, "companion control unavailable", http.StatusServiceUnavailable)
			return
		}
		defer connection.Close()
		response, err := controlpb.NewCompanionControlClient(connection).Inspect(ctx, &controlpb.InspectRequest{})
		if err != nil || response.GetGeneration() == "" {
			http.Error(w, "companion control unavailable", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	})
	return mux
}

func serve(ctx context.Context, socket, binary, listen, healthListen string, supervisor *supervision.Supervisor) error {
	if err := os.MkdirAll(filepath.Dir(socket), 0o700); err != nil {
		return err
	}
	if err := os.Remove(socket); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		return err
	}
	defer listener.Close()
	if err := os.Chmod(socket, 0o600); err != nil {
		return err
	}
	healthListener, err := net.Listen("tcp", healthListen)
	if err != nil {
		return err
	}
	defer healthListener.Close()
	lifetime, cancel := context.WithCancel(ctx)
	defer cancel()
	state := &companionOwnership{
		status:        companionStatus{Generation: uuid.NewString()},
		fenceRequests: make(chan struct{}, 1),
	}
	go state.fenceLoop(lifetime, cancel, supervisor)
	controlServer := grpc.NewServer(grpc.MaxRecvMsgSize(1 << 20))
	controlpb.RegisterCompanionControlServer(controlServer, &companionControl{
		lifetime: lifetime, cancel: cancel, state: state, binary: binary, listen: listen, supervisor: supervisor,
	})
	healthServerGRPC := health.NewServer()
	healthServerGRPC.SetServingStatus(controlpb.CompanionControl_ServiceDesc.ServiceName, healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(controlServer, healthServerGRPC)
	healthServer := &http.Server{
		Handler:           healthHandler(socket),
		ReadHeaderTimeout: time.Second,
		WriteTimeout:      2 * time.Second,
		IdleTimeout:       5 * time.Second,
		BaseContext:       func(net.Listener) context.Context { return lifetime },
	}
	stop := context.AfterFunc(lifetime, func() {
		controlServer.Stop()
		_ = healthServer.Close()
	})
	defer stop()
	var servers errgroup.Group
	servers.Go(func() error {
		defer cancel()
		err := healthServer.Serve(healthListener)
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	})
	servers.Go(func() error {
		defer cancel()
		return controlServer.Serve(listener)
	})
	return servers.Wait()
}

type companionStatus struct {
	Generation string
	Owned      bool
}

type companionControl struct {
	controlpb.UnimplementedCompanionControlServer
	lifetime   context.Context
	cancel     context.CancelFunc
	state      *companionOwnership
	binary     string
	listen     string
	supervisor *supervision.Supervisor
}

func (s *companionControl) Inspect(_ context.Context, _ *controlpb.InspectRequest) (*controlpb.InspectResponse, error) {
	s.state.mu.Lock()
	defer s.state.mu.Unlock()
	return &controlpb.InspectResponse{Generation: s.state.status.Generation, Owned: s.state.status.Owned}, nil
}

func (s *companionControl) Fence(_ context.Context, request *controlpb.FenceRequest) (*controlpb.FenceResponse, error) {
	if request.GetGeneration() == "" {
		return nil, status.Error(codes.InvalidArgument, "fence requires a generation")
	}
	s.state.requestFence(request.GetGeneration())
	return &controlpb.FenceResponse{}, nil
}

func (s *companionControl) Claim(request *controlpb.ClaimRequest, stream grpc.ServerStreamingServer[controlpb.ClaimResponse]) error {
	s.state.mu.Lock()
	if s.state.status.Owned || s.state.fencing {
		s.state.mu.Unlock()
		s.cancel()
		return status.Error(codes.Aborted, "executor companion already owned or fencing")
	}
	process, err := s.supervisor.NewProcess(supervision.ProcessConfig{
		Command:   []string{s.binary, "exec-server", "--listen", s.listen},
		Env:       request.GetEnvironment(),
		StopGrace: 5 * time.Second,
	})
	if err != nil {
		s.state.mu.Unlock()
		return status.Error(codes.FailedPrecondition, err.Error())
	}
	if err := process.Start(s.lifetime); err != nil {
		// No process started, so this idle generation does not need a fence.
		fmt.Fprintln(os.Stderr, "start Codex executor:", err)
		s.state.mu.Unlock()
		return status.Error(codes.FailedPrecondition, err.Error()+"; "+process.ExitDescription())
	}
	expectedExit := &atomic.Bool{}
	leaseDone := make(chan struct{})
	s.state.status.Owned = true
	s.state.process = process
	s.state.lease = leaseDone
	s.state.expectedExit = expectedExit
	generation := s.state.status.Generation
	s.state.mu.Unlock()
	// Cancellation, transport loss, or an incomplete handshake must fence this owner.
	defer s.state.requestFence(generation)
	go forwardOutput(s.lifetime, s.cancel, "stdout", process.Stdout(), os.Stdout)
	go forwardOutput(s.lifetime, s.cancel, "stderr", process.Stderr(), os.Stderr)
	if err := process.CloseStdin(); err != nil {
		fmt.Fprintln(os.Stderr, "close Codex executor stdin:", err)
		s.cancel()
		return status.Error(codes.Unavailable, "could not close executor stdin")
	}
	go waitForExecutor(process, expectedExit, func() { s.state.unexpectedExit(process) })
	if err := streaming.SendWithin(stream.Context(), 3*time.Second, stream.Send, &controlpb.ClaimResponse{Generation: generation}); err != nil {
		// As with a failed claim response before gRPC, an unconfirmed owner exits the container.
		s.cancel()
		return status.Error(codes.Unavailable, "could not send executor ownership")
	}
	select {
	case <-stream.Context().Done():
		return stream.Context().Err()
	case <-leaseDone:
		return nil
	case <-s.lifetime.Done():
		return status.Error(codes.Unavailable, "executor companion is stopping")
	}
}

type companionOwnership struct {
	mu            sync.Mutex
	status        companionStatus
	process       *supervision.Process
	lease         chan struct{}
	expectedExit  *atomic.Bool
	fencing       bool
	pendingFence  string
	fenceRequests chan struct{}
}

func (s *companionOwnership) requestFence(generation string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if generation == "" || s.status.Generation != generation || !s.status.Owned || s.fencing || s.pendingFence == generation {
		return
	}
	s.pendingFence = generation
	select {
	case s.fenceRequests <- struct{}{}:
	default:
		// A previous wake-up will consume pendingFence. Keep the newest generation.
	}
}

func (s *companionOwnership) unexpectedExit(process *supervision.Process) {
	s.mu.Lock()
	unexpected := s.process == process && s.expectedExit != nil && !s.expectedExit.Load()
	generation := s.status.Generation
	s.mu.Unlock()
	if unexpected {
		s.requestFence(generation)
	}
}

func (s *companionOwnership) fenceLoop(
	lifetime context.Context,
	cancel context.CancelFunc,
	supervisor *supervision.Supervisor,
) {
	for {
		select {
		case <-lifetime.Done():
			return
		case <-s.fenceRequests:
			s.mu.Lock()
			generation := s.pendingFence
			s.pendingFence = ""
			if generation == "" || s.status.Generation != generation || !s.status.Owned || s.fencing {
				s.mu.Unlock()
				continue
			}
			process := s.process
			if process == nil || s.expectedExit == nil {
				s.mu.Unlock()
				fmt.Fprintln(os.Stderr, "owned Codex executor has no process lifecycle handle")
				cancel()
				return
			}
			s.fencing = true
			s.expectedExit.Store(true)
			lease := s.lease
			s.mu.Unlock()

			fenceCtx, finish := context.WithTimeout(lifetime, 30*time.Second)
			err := process.Stop(fenceCtx)
			if err == nil {
				err = supervisor.FenceNamespace(fenceCtx)
			}
			finish()
			if err != nil {
				fmt.Fprintln(os.Stderr, "fence Codex executor process domain:", err)
				// An incomplete in-container fence falls back to the runtime's
				// whole-namespace teardown.
				cancel()
				return
			}

			s.mu.Lock()
			if s.process != process {
				s.mu.Unlock()
				fmt.Fprintln(os.Stderr, "Codex executor changed during process-domain fence")
				cancel()
				return
			}
			s.status.Generation = uuid.NewString()
			s.status.Owned = false
			s.process = nil
			s.lease = nil
			s.expectedExit = nil
			s.fencing = false
			s.mu.Unlock()
			if lease != nil {
				close(lease)
			}
		}
	}
}

func waitForExecutor(process *supervision.Process, expectedExit *atomic.Bool, unexpectedExit func()) {
	code, err := process.Wait(context.Background())
	if err != nil {
		fmt.Fprintln(os.Stderr, "Codex executor cleanup failed:", err)
	} else {
		fmt.Fprintln(os.Stderr, "Codex executor exited with status:", code)
	}
	if !expectedExit.Load() {
		unexpectedExit()
	}
}

// Process streams can outlive the direct child when an escaped descendant inherits them. PID 1
// must be able to return so the container runtime can fence those descendants.
func forwardOutput(ctx context.Context, cancel context.CancelFunc, name string, source io.Reader, destination io.Writer) {
	if _, err := io.Copy(destination, source); err != nil && ctx.Err() == nil {
		fmt.Fprintf(os.Stderr, "copy Codex executor %s: %v\n", name, err)
		cancel()
	}
}
