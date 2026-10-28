package runner

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/google/uuid"
	"golang.org/x/sys/unix"
)

const (
	codexExecServerContainerMode       = "--executor-container"
	codexExecServerContainerHealthMode = "--executor-container-health"
)

// CodexExecServerContainerMain must be PID 1 in a private container PID namespace.
// Returning to main exits that namespace, letting the runtime kill even children
// that escaped their process group. This mode never runs as an ordinary VM child.
func CodexExecServerContainerMain(args []string) int {
	flags := flag.NewFlagSet("executor container", flag.ContinueOnError)
	socket := flags.String("socket", "", "local ownership socket")
	listen := flags.String("listen", "", "Codex WebSocket endpoint")
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
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	if err := serveCodexExecServerContainer(ctx, *socket, *binary, *listen); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}

func CodexExecServerContainerHealthMain(args []string) int {
	if len(args) != 1 {
		return 64
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	c, _, err := codexExecServerControl(ctx, args[0], codexExecServerControlRequest{Operation: "inspect"})
	if err != nil {
		return 1
	}
	_ = c.Close()
	return 0
}

func serveCodexExecServerContainer(ctx context.Context, socket, binary, listen string) error {
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
	lifetime, cancel := context.WithCancel(ctx)
	defer cancel()
	stop := context.AfterFunc(lifetime, func() { _ = listener.Close() })
	defer stop()
	status := codexExecServerControlStatus{Generation: uuid.NewString()}
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
