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
	"os/exec"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/executor/control"
	"github.com/google/uuid"
	"golang.org/x/sync/errgroup"
	"golang.org/x/sys/unix"
)

// Main must be PID 1 in a private container PID namespace.
// Returning to main exits that namespace, letting the runtime kill even children
// that escaped their process group. This mode never runs as an ordinary VM child.
func Main(args []string) int {
	flags := flag.NewFlagSet("agentplane-runner executor", flag.ContinueOnError)
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
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	if err := serve(ctx, *socket, *binary, *listen, *healthListen); err != nil {
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

func serve(ctx context.Context, socket, binary, listen, healthListen string) error {
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
		return serveControl(lifetime, cancel, listener, binary, listen)
	})
	return servers.Wait()
}

func serveControl(lifetime context.Context, cancel context.CancelFunc, listener net.Listener, binary, listen string) error {
	status := control.Status{Generation: uuid.NewString()}
	var mu sync.Mutex
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
			mu.Lock()
			if request.Operation == "claim" {
				if status.Owned {
					mu.Unlock()
					cancel()
					return
				}
				child := exec.Command(binary, "exec-server", "--listen", listen)
				child.Env = environment(request.Environment)
				child.Stdout, child.Stderr = os.Stdout, os.Stderr
				child.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
				if err := child.Start(); err != nil {
					// Nothing started, so this generation has no work to fence. Refuse and keep serving,
					// allowing Runner to retry or fence without waiting for a container restart.
					fmt.Fprintln(os.Stderr, "start Codex executor:", err)
					refused := status
					refused.Error = err.Error()
					mu.Unlock()
					_ = json.NewEncoder(c).Encode(refused)
					return
				}
				status.Owned = true
				go reap(lifetime, child.Process, cancel)
			} else if request.Operation != "inspect" || len(request.Environment) != 0 {
				mu.Unlock()
				return
			}
			err := json.NewEncoder(c).Encode(status)
			mu.Unlock()
			if err != nil {
				if request.Operation == "claim" {
					cancel()
				}
				return
			}
			_ = c.SetDeadline(time.Time{})
			if request.Operation == "claim" {
				// Any input, EOF or IO error ends this container generation.
				var one [1]byte
				_, _ = c.Read(one[:])
				cancel()
				return
			}
			_ = c.SetReadDeadline(time.Now().Add(3 * time.Second))
			var next control.Request
			if decoder.Decode(&next) == nil && next.Operation == "fence" {
				cancel()
			}
		}(connection)
	}
}

// PID 1 reaps orphaned grandchildren. One wait4 owner avoids racing os/exec.Wait with the reaper.
func reap(ctx context.Context, process *os.Process, exited context.CancelFunc) {
	defer process.Release()
	changed := make(chan os.Signal, 1)
	signal.Notify(changed, syscall.SIGCHLD)
	defer signal.Stop(changed)
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	for {
		for {
			var status unix.WaitStatus
			pid, err := unix.Wait4(-1, &status, unix.WNOHANG, nil)
			if pid == process.Pid {
				fmt.Fprintln(os.Stderr, "Codex executor exited:", status)
				exited()
				return
			}
			if err != nil || pid == 0 {
				break
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-changed:
		case <-ticker.C:
		}
	}
}

// The server gives exec-server a minimal, deterministic environment from Runner's claim.
func environment(values map[string]string) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	result := make([]string, 0, len(keys))
	for _, key := range keys {
		result = append(result, key+"="+values[key])
	}
	return result
}
