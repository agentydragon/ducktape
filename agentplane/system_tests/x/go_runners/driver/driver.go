// Package driver owns the test-only stack host used by both runner spikes.
package driver

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"

	"github.com/bazelbuild/rules_go/go/runfiles"
)

type Handle struct {
	URL     string `json:"url"`
	StackID string `json:"stack_id"`
}

type Host struct {
	Handle
	command *exec.Cmd
	done    chan error
}

type Event struct {
	Event   string `json:"event"`
	Case    string `json:"case"`
	StackID string `json:"stack_id"`
	UID     string `json:"uid"`
}

type Snapshot struct {
	StackID string   `json:"stack_id"`
	Active  []string `json:"active"`
	Events  []Event  `json:"events"`
}

func Mode() string {
	if mode := os.Getenv("RUNNER_SPIKE_MODE"); mode != "" {
		return mode
	}
	return "probe"
}

func Cases() []string {
	if Mode() == "vm" {
		return []string{"claude", "codex", "codex-oom"}
	}
	return []string{"a", "b", "c", "d", "e", "f"}
}

func Start(ctx context.Context) (*Host, error) {
	binary, err := runfiles.Rlocation("_main/agentplane/system_tests/x/go_runners/host_bin")
	if err != nil {
		return nil, err
	}
	directory, err := os.MkdirTemp(os.Getenv("TEST_TMPDIR"), "stack-host-")
	if err != nil {
		return nil, err
	}
	output := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if output == "" {
		output = directory
	}
	log, err := os.Create(filepath.Join(output, "stack-host.log"))
	if err != nil {
		return nil, err
	}
	ready := filepath.Join(directory, "ready.json")
	command := exec.Command(binary, "--ready-file", ready, "--mode", Mode())
	command.Stdout, command.Stderr = log, log
	if err := command.Start(); err != nil {
		_ = log.Close()
		return nil, err
	}
	host := &Host{command: command, done: make(chan error, 1)}
	go func() {
		host.done <- command.Wait()
		_ = log.Close()
	}()
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()
	for {
		if data, err := os.ReadFile(ready); err == nil {
			if err := json.Unmarshal(data, &host.Handle); err == nil && host.URL != "" {
				return host, nil
			}
		}
		select {
		case err := <-host.done:
			return nil, fmt.Errorf("stack host exited before readiness: %v; see stack-host.log", err)
		case <-ctx.Done():
			_ = command.Process.Kill()
			<-host.done
			return nil, fmt.Errorf("stack startup: %w; see stack-host.log", ctx.Err())
		case <-ticker.C:
		}
	}
}

func (h Handle) request(ctx context.Context, method, path string, result any) error {
	request, err := http.NewRequestWithContext(ctx, method, h.URL+path, nil)
	if err != nil {
		return err
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return err
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(response.Body, 8192))
		return fmt.Errorf("%s: HTTP %d: %s", path, response.StatusCode, body)
	}
	if result != nil {
		return json.NewDecoder(response.Body).Decode(result)
	}
	_, err = io.Copy(io.Discard, response.Body)
	return err
}

func (h Handle) Begin(ctx context.Context, name string) error {
	var received Handle
	if err := h.request(ctx, http.MethodPost, "/begin/"+name, &received); err != nil {
		return err
	}
	if received.StackID != h.StackID {
		return fmt.Errorf("case %q connected to another stack", name)
	}
	return nil
}

func (h Handle) Run(ctx context.Context, name string) error {
	return h.request(ctx, http.MethodPost, "/run/"+name, nil)
}

func (h Handle) Cleanup(ctx context.Context, name string) error {
	return h.request(ctx, http.MethodPost, "/cleanup/"+name, nil)
}

func (h Handle) Snapshot(ctx context.Context) (Snapshot, error) {
	var snapshot Snapshot
	err := h.request(ctx, http.MethodGet, "/snapshot", &snapshot)
	return snapshot, err
}

func (h *Host) Stop(ctx context.Context) error {
	err := h.request(ctx, http.MethodPost, "/shutdown", nil)
	if err != nil {
		_ = h.command.Process.Signal(syscall.SIGTERM)
	}
	select {
	case exit := <-h.done:
		if err != nil {
			return err
		}
		return exit
	case <-ctx.Done():
		_ = h.command.Process.Kill()
		<-h.done
		return fmt.Errorf("stack shutdown: %w", ctx.Err())
	}
}

func (h Handle) Verify(ctx context.Context) error {
	snapshot, err := h.Snapshot(ctx)
	if err != nil {
		return err
	}
	if snapshot.StackID != h.StackID || len(snapshot.Active) != 0 {
		return fmt.Errorf("stack identity or cleanup failed: %+v", snapshot)
	}
	starts, begins, closes := 0, 0, 0
	active, peak := 0, 0
	boots := map[string]bool{}
	for _, event := range snapshot.Events {
		if event.StackID != h.StackID {
			return fmt.Errorf("event from another stack: %+v", event)
		}
		switch event.Event {
		case "stack-start":
			starts++
		case "case-begin":
			begins++
			active++
			peak = max(peak, active)
		case "case-close":
			closes++
			active--
		case "sandbox-ready":
			boots[event.UID] = true
		}
	}
	if starts != 1 || begins == 0 || closes != begins || peak != min(3, begins) {
		return fmt.Errorf("unexpected lifecycle: starts=%d begun=%d closed=%d peak=%d", starts, begins, closes, peak)
	}
	if Mode() == "vm" && len(boots) != begins {
		return fmt.Errorf("expected %d independent VM Sandboxes, got %d", begins, len(boots))
	}
	return nil
}

// CaseTimeout bounds the cancellation probe without changing the real VM deadline.
func CaseTimeout(name string) time.Duration {
	if Mode() == "probe" && os.Getenv("RUNNER_SPIKE_FAULT") == "timeout" && name == "a" {
		return time.Second
	}
	return 10 * time.Minute
}
