package executor

import (
	"context"
	"encoding/json"
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

	"github.com/agentydragon/ducktape/agentplane/env/execution/companion/control"
	"github.com/agentydragon/ducktape/agentplane/env/supervision"
	"github.com/google/uuid"
	"golang.org/x/sync/errgroup"
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
	supervisor, err := supervision.NewPID1SupervisorWithoutStateOwner()
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
		connection, _, err := control.Connect(ctx, socket, control.Request{Operation: "inspect"})
		if err != nil {
			http.Error(w, "companion control unavailable", http.StatusServiceUnavailable)
			return
		}
		_ = connection.Close()
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
	healthServer := &http.Server{
		Handler:           healthHandler(socket),
		ReadHeaderTimeout: time.Second,
		WriteTimeout:      2 * time.Second,
		IdleTimeout:       5 * time.Second,
		BaseContext:       func(net.Listener) context.Context { return lifetime },
	}
	stop := context.AfterFunc(lifetime, func() {
		_ = listener.Close()
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
		return serveControl(lifetime, cancel, listener, binary, listen, supervisor)
	})
	return servers.Wait()
}

func serveControl(
	lifetime context.Context,
	cancel context.CancelFunc,
	listener net.Listener,
	binary, listen string,
	supervisor *supervision.Supervisor,
) error {
	state := &companionOwnership{
		status:        control.Status{Generation: uuid.NewString()},
		fenceRequests: make(chan struct{}, 1),
	}
	go state.fenceLoop(lifetime, cancel, supervisor)
	for {
		connection, err := listener.Accept()
		if lifetime.Err() != nil {
			return nil
		}
		if err != nil {
			return err
		}
		go func(c net.Conn) {
			defer c.Close()
			closeOnExit := context.AfterFunc(lifetime, func() { _ = c.Close() })
			defer closeOnExit()
			_ = c.SetDeadline(time.Now().Add(3 * time.Second))
			decoder := json.NewDecoder(io.LimitReader(c, 1<<20))
			decoder.DisallowUnknownFields()
			var request control.Request
			if decoder.Decode(&request) != nil {
				return
			}
			var responseStatus control.Status
			if request.Operation == "claim" {
				state.mu.Lock()
				if state.status.Owned || state.fencing {
					state.mu.Unlock()
					cancel()
					return
				}
				process, err := supervisor.NewProcess(supervision.ProcessConfig{
					Command: []string{binary, "exec-server", "--listen", listen},
					Env:     request.Environment,
				})
				if err != nil {
					refused := state.status
					refused.Error = err.Error()
					state.mu.Unlock()
					_ = json.NewEncoder(c).Encode(refused)
					return
				}
				if err := process.Start(lifetime); err != nil {
					// Nothing started, so this generation has no work to fence. Refuse and keep serving,
					// allowing Runner to retry or fence without waiting for a container restart.
					fmt.Fprintln(os.Stderr, "start Codex executor:", err)
					refused := state.status
					refused.Error = err.Error() + "; " + process.ExitDescription()
					state.mu.Unlock()
					_ = json.NewEncoder(c).Encode(refused)
					return
				}
				expectedExit := &atomic.Bool{}
				state.status.Owned = true
				state.status.Error = ""
				state.process = process
				state.lease = c
				state.expectedExit = expectedExit
				responseStatus = state.status
				state.mu.Unlock()
				go forwardOutput(lifetime, cancel, "stdout", process.Stdout(), os.Stdout)
				go forwardOutput(lifetime, cancel, "stderr", process.Stderr(), os.Stderr)
				if err := process.CloseStdin(); err != nil {
					fmt.Fprintln(os.Stderr, "close Codex executor stdin:", err)
					cancel()
					return
				}
				go waitForExecutor(process, expectedExit, func() { state.unexpectedExit(process) })
			} else if (request.Operation != "inspect" && request.Operation != "fence") || len(request.Environment) != 0 {
				return
			} else {
				state.mu.Lock()
				responseStatus = state.status
				state.mu.Unlock()
			}
			err := json.NewEncoder(c).Encode(responseStatus)
			if err != nil {
				if request.Operation == "claim" {
					cancel()
				}
				return
			}
			_ = c.SetDeadline(time.Time{})
			if request.Operation == "claim" {
				// Closing the lease requests a process-domain fence. PID 1 keeps
				// serving only after the workload is proven absent.
				var one [1]byte
				_, _ = c.Read(one[:])
				state.requestFence()
				return
			}
			if request.Operation == "fence" {
				state.requestFence()
				return
			}
			_ = c.SetReadDeadline(time.Now().Add(3 * time.Second))
			var next control.Request
			if decoder.Decode(&next) == nil && next.Operation == "fence" {
				state.requestFence()
			}
		}(connection)
	}
}

type companionOwnership struct {
	mu            sync.Mutex
	status        control.Status
	process       *supervision.Process
	lease         net.Conn
	expectedExit  *atomic.Bool
	fencing       bool
	fenceRequests chan struct{}
}

func (s *companionOwnership) requestFence() {
	select {
	case s.fenceRequests <- struct{}{}:
	default:
	}
}

func (s *companionOwnership) unexpectedExit(process *supervision.Process) {
	s.mu.Lock()
	unexpected := s.process == process && s.expectedExit != nil && !s.expectedExit.Load()
	s.mu.Unlock()
	if unexpected {
		s.requestFence()
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
			if !s.status.Owned || s.fencing {
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
			err := process.Stop(fenceCtx, 5*time.Second)
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
			s.status.Error = ""
			s.process = nil
			s.lease = nil
			s.expectedExit = nil
			s.fencing = false
			s.mu.Unlock()
			if lease != nil {
				_ = lease.Close()
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
