package centralsampler

import (
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"time"
)

type Host struct {
	cmd       *exec.Cmd
	stdin     io.WriteCloser
	stdout    io.ReadCloser
	log       *os.File
	done      chan struct{}
	waitErr   error
	mu        sync.Mutex
	closeOnce sync.Once
	closeErr  error
}

func StartHost(ctx context.Context, binary, logPath string) (*Host, error) {
	if binary == "" || logPath == "" {
		return nil, errors.New("code-mode binary and log path are required")
	}
	if err := os.MkdirAll(filepath.Dir(logPath), 0o750); err != nil {
		return nil, err
	}
	log, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o640)
	if err != nil {
		return nil, err
	}
	host := &Host{log: log, done: make(chan struct{})}
	command := exec.CommandContext(ctx, binary, "--listen", "stdio")
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.Cancel = func() error {
		if command.Process == nil {
			return os.ErrProcessDone
		}
		return killProcessGroup(command.Process.Pid)
	}
	stdin, err := command.StdinPipe()
	if err != nil {
		_ = log.Close()
		return nil, err
	}
	stdout, err := command.StdoutPipe()
	if err != nil {
		_ = stdin.Close()
		_ = log.Close()
		return nil, err
	}
	command.Stderr = log
	if err := command.Start(); err != nil {
		_ = stdin.Close()
		_ = stdout.Close()
		_ = log.Close()
		return nil, err
	}
	host.cmd, host.stdin, host.stdout = command, stdin, stdout
	go func() {
		host.waitErr = command.Wait()
		close(host.done)
	}()
	return host, nil
}

func killProcessGroup(pid int) error {
	if err := syscall.Kill(-pid, syscall.SIGKILL); err != nil {
		if errors.Is(err, syscall.ESRCH) {
			return os.ErrProcessDone
		}
		return err
	}
	return nil
}

func (h *Host) Stdin() io.WriteCloser { return h.stdin }
func (h *Host) Stdout() io.ReadCloser { return h.stdout }
func (h *Host) PID() int              { return h.cmd.Process.Pid }
func (h *Host) Done() <-chan struct{} { return h.done }

func (h *Host) Wait() error {
	<-h.done
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.waitErr
}

func (h *Host) Kill() error {
	if h.cmd.Process == nil {
		return nil
	}
	// Keep targeting the original process group after its leader exits: a child
	// may still be running in that group. The group ID cannot be reused while
	// such a child remains a member.
	return killProcessGroup(h.cmd.Process.Pid)
}

func (h *Host) Close() error {
	h.closeOnce.Do(func() {
		killErr := h.Kill()
		select {
		case <-h.done:
			var errs []error
			if killErr != nil && !errors.Is(killErr, os.ErrProcessDone) {
				errs = append(errs, killErr)
			}
			if err := h.stdin.Close(); err != nil && !errors.Is(err, os.ErrClosed) {
				errs = append(errs, err)
			}
			if err := h.stdout.Close(); err != nil && !errors.Is(err, os.ErrClosed) {
				errs = append(errs, err)
			}
			if err := h.log.Close(); err != nil && !errors.Is(err, os.ErrClosed) {
				errs = append(errs, err)
			}
			h.closeErr = errors.Join(errs...)
		case <-time.After(5 * time.Second):
			_ = h.stdin.Close()
			_ = h.stdout.Close()
			_ = h.log.Close()
			h.closeErr = errors.Join(killErr, errors.New("code-mode host did not stop after kill"))
		}
	})
	return h.closeErr
}
