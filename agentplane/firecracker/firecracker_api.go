package firecracker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"sync"
	"syscall"
	"time"
)

const (
	apiTimeout          = 20 * time.Second
	snapshotAPITimeout  = 2 * time.Minute
	maxAPIResponseBytes = 1 << 20
	vmmStopGrace        = 5 * time.Second
)

type API struct {
	client *http.Client
}

func NewAPI(socket string, timeout time.Duration) *API {
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		},
		DisableKeepAlives: true,
	}
	return &API{client: &http.Client{Transport: transport, Timeout: timeout}}
}

func (a *API) Request(ctx context.Context, method, route string, payload any) error {
	var body io.Reader
	if method != http.MethodGet {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return fmt.Errorf("encode Firecracker %s %s payload: %w", method, route, err)
		}
		body = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, "http://firecracker"+route, body)
	if err != nil {
		return fmt.Errorf("create Firecracker %s %s request: %w", method, route, err)
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := a.client.Do(request)
	if err != nil {
		return fmt.Errorf("Firecracker %s %s: %w", method, route, err)
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, maxAPIResponseBytes+1))
	if err != nil {
		return fmt.Errorf("read Firecracker %s %s response: %w", method, route, err)
	}
	if len(responseBody) > maxAPIResponseBytes {
		return fmt.Errorf("Firecracker %s %s response exceeds %d bytes", method, route, maxAPIResponseBytes)
	}
	if response.StatusCode >= http.StatusMultipleChoices {
		return fmt.Errorf("Firecracker %s %s: HTTP %d: %s", method, route, response.StatusCode, responseBody)
	}
	return nil
}

type VMM struct {
	command *exec.Cmd
	done    chan struct{}

	mu      sync.RWMutex
	exitErr error
	closeMu sync.Mutex
}

func StartVMM(binary, socket, log string) (*VMM, error) {
	output, err := os.OpenFile(log, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return nil, fmt.Errorf("open Firecracker log %s: %w", log, err)
	}
	command := exec.Command(binary, "--api-sock", socket)
	command.Stdout = output
	command.Stderr = output
	command.Stdin = nil
	if err := command.Start(); err != nil {
		return nil, errors.Join(fmt.Errorf("start Firecracker %s: %w", binary, err), output.Close())
	}
	if err := output.Close(); err != nil {
		_ = command.Process.Kill()
		waitErr := command.Wait()
		return nil, errors.Join(fmt.Errorf("close Firecracker log %s: %w", log, err), waitErr)
	}
	vmm := &VMM{command: command, done: make(chan struct{})}
	go vmm.wait()
	return vmm, nil
}

func (v *VMM) Done() <-chan struct{} {
	return v.done
}

func (v *VMM) ExitError() error {
	v.mu.RLock()
	defer v.mu.RUnlock()
	return v.exitErr
}

func (v *VMM) wait() {
	err := v.command.Wait()
	v.mu.Lock()
	v.exitErr = err
	v.mu.Unlock()
	close(v.done)
}

func (v *VMM) Close() error {
	return v.closeAfter(vmmStopGrace)
}

func (v *VMM) closeAfter(grace time.Duration) error {
	v.closeMu.Lock()
	defer v.closeMu.Unlock()
	select {
	case <-v.done:
		return nil
	default:
	}
	var stopErr error
	if err := v.command.Process.Signal(syscall.SIGTERM); err != nil && !errors.Is(err, os.ErrProcessDone) {
		stopErr = fmt.Errorf("send SIGTERM to Firecracker: %w", err)
	}
	timer := time.NewTimer(grace)
	defer timer.Stop()
	select {
	case <-v.done:
		return stopErr
	case <-timer.C:
	}
	if err := v.command.Process.Kill(); err != nil && !errors.Is(err, os.ErrProcessDone) {
		stopErr = errors.Join(stopErr, fmt.Errorf("kill Firecracker after SIGTERM timeout: %w", err))
	}
	<-v.done
	return stopErr
}
