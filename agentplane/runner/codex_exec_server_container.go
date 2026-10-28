package runner

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/google/uuid"
	"golang.org/x/sys/unix"
)

const (
	codexExecServerContainerMode = "--executor-container"
	executorHealthPath           = "/healthz"
	executorHealthTimeout        = time.Second
)

// CodexExecServerContainerMain must be PID 1 in a private container PID namespace.
// Returning to main exits that namespace, letting the runtime kill even children
// that escaped their process group. This mode never runs as an ordinary VM child.
func CodexExecServerContainerMain(args []string) int {
	flags := flag.NewFlagSet("executor container", flag.ContinueOnError)
	socket := flags.String("socket", "", "local ownership socket")
	listen := flags.String("listen", "", "Codex WebSocket endpoint")
	healthListen := flags.String("health-listen", "", "HTTP health listener address")
	binary := flags.String("binary", "", "Codex executable")
	if err := flags.Parse(args); err != nil {
		return 64
	}
	if os.Getpid() != 1 || !filepath.IsAbs(*socket) || !filepath.IsAbs(*binary) || flags.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "executor companion requires PID 1, absolute socket and binary paths, and a private PID namespace")
		return 64
	}
	if err := validateExecutorURL(*listen); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 64
	}
	if err := validateExecutorHealthAddress(*healthListen); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 64
	}
	healthListener, err := net.Listen("tcp", *healthListen)
	if err != nil {
		fmt.Fprintln(os.Stderr, "listen for executor health:", err)
		return 1
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	if err := serveCodexExecServerContainer(ctx, *socket, *binary, *listen, healthListener); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}

func validateExecutorHealthAddress(address string) error {
	host, portText, err := net.SplitHostPort(address)
	if err != nil || net.ParseIP(host) == nil {
		return fmt.Errorf("executor health listener must be an IP address and port: %q", address)
	}
	port, err := strconv.Atoi(portText)
	if err != nil || port < 1 || port > 65535 {
		return fmt.Errorf("executor health listener must use a port from 1 to 65535: %q", address)
	}
	return nil
}

func serveCodexExecServerContainer(ctx context.Context, socket, binary, listen string, healthListener net.Listener) error {
	if healthListener == nil {
		return errors.New("executor health listener is required")
	}
	defer healthListener.Close()
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
	healthServer := &http.Server{
		Handler:           executorHealthHandler(socket),
		ReadHeaderTimeout: executorHealthTimeout,
		WriteTimeout:      2 * executorHealthTimeout,
	}
	lifetime, cancel := context.WithCancel(ctx)
	defer cancel()
	healthServerErrors := make(chan error, 1)
	go func() {
		err := healthServer.Serve(healthListener)
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			healthServerErrors <- err
			cancel()
		}
	}()
	stop := context.AfterFunc(lifetime, func() {
		_ = listener.Close()
		_ = healthServer.Close()
	})
	defer func() {
		stop()
		_ = listener.Close()
		_ = healthServer.Close()
	}()
	status := codexExecServerControlStatus{Generation: uuid.NewString()}
	var mu sync.Mutex
	for {
		connection, err := listener.Accept()
		if lifetime.Err() != nil {
			select {
			case healthErr := <-healthServerErrors:
				return fmt.Errorf("executor health server: %w", healthErr)
			default:
			}
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
			var request codexExecServerControlRequest
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
					// Nothing was spawned, so this generation holds no work to fence. Refuse the claim
					// with the reason and keep serving: exiting here makes the runtime restart a container
					// whose next start fails the same way, and a claimant in restart backoff leaves no
					// socket to fence, so the Runner can neither start nor quiesce.
					fmt.Fprintln(os.Stderr, "start Codex exec-server:", err)
					refused := status
					refused.Error = err.Error()
					mu.Unlock()
					_ = json.NewEncoder(c).Encode(refused)
					return
				}
				status.Owned = true
				go reapCodexExecServerContainer(lifetime, child.Process, cancel)
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
			var next codexExecServerControlRequest
			if decoder.Decode(&next) == nil && next.Operation == "fence" {
				cancel()
			}
		}(connection)
	}
}

func executorHealthHandler(socket string) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc(executorHealthPath, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != executorHealthPath {
			http.NotFound(w, r)
			return
		}
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), executorHealthTimeout)
		defer cancel()
		connection, _, err := codexExecServerControl(ctx, socket, codexExecServerControlRequest{Operation: "inspect"})
		if err != nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		_ = connection.Close()
		w.WriteHeader(http.StatusOK)
	})
	return mux
}

// PID 1 also reaps orphaned grandchildren. A single wait4 owner avoids races
// between os/exec.Wait and a generic orphan reaper consuming the executor status.
func reapCodexExecServerContainer(ctx context.Context, process *os.Process, exited context.CancelFunc) {
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
				fmt.Fprintln(os.Stderr, "Codex exec-server exited:", status)
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
