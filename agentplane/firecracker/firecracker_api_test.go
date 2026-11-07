package firecracker

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestAPIRequestUsesUnixSocketAndIgnoresSuccessfulResponseBody(t *testing.T) {
	socket := filepath.Join(t.TempDir(), "firecracker.sock")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	requests := make(chan receivedRequest, 2)
	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		requests <- receivedRequest{method: r.Method, route: r.URL.RequestURI(), contentType: r.Header.Get("Content-Type"), body: string(body)}
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, `{"ignored":"response bodies remain empty at the API boundary"}`)
	})}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { _ = server.Close() })

	api := NewAPI(socket, apiTimeout)
	if err := api.Request(context.Background(), http.MethodPut, "/machine-config", map[string]any{"vcpu_count": 2}); err != nil {
		t.Fatal(err)
	}
	if err := api.Request(context.Background(), http.MethodGet, "/vm", nil); err != nil {
		t.Fatal(err)
	}
	if got, want := <-requests, (receivedRequest{method: "PUT", route: "/machine-config", contentType: "application/json", body: `{"vcpu_count":2}`}); got != want {
		t.Errorf("PUT request = %+v, want %+v", got, want)
	}
	if got, want := <-requests, (receivedRequest{method: "GET", route: "/vm", contentType: "application/json"}); got != want {
		t.Errorf("GET request = %+v, want %+v", got, want)
	}
}

func TestAPIRequestRejectsHTTPFailureAndOversizedResponse(t *testing.T) {
	socket := filepath.Join(t.TempDir(), "firecracker.sock")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/failure":
			http.Error(w, "rejected", http.StatusBadRequest)
		case "/large":
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(make([]byte, maxAPIResponseBytes+1))
		}
	})}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { _ = server.Close() })

	api := NewAPI(socket, apiTimeout)
	if err := api.Request(context.Background(), http.MethodPut, "/failure", nil); err == nil || !strings.Contains(err.Error(), "HTTP 400") {
		t.Errorf("HTTP failure error = %v, want status error", err)
	}
	if err := api.Request(context.Background(), http.MethodGet, "/large", nil); err == nil || !strings.Contains(err.Error(), "exceeds") {
		t.Errorf("oversized response error = %v, want bounded-response error", err)
	}
}

type receivedRequest struct {
	method      string
	route       string
	contentType string
	body        string
}

func TestStartVMMOwnsProcessAndCloseIsIdempotent(t *testing.T) {
	dir := t.TempDir()
	marker := filepath.Join(dir, "started")
	binary := writeShell(t, dir, "vmm", fmt.Sprintf("trap 'exit 0' TERM\nprintf '%%s %%s\\n' \"$1\" \"$2\" > %s\nwhile :; do :; done\n", shellQuote(marker)))
	socket := filepath.Join(dir, "api.sock")
	log := filepath.Join(dir, "firecracker.log")
	vmm, err := StartVMM(binary, socket, log)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := vmm.Close(); err != nil {
			t.Errorf("close VMM during cleanup: %v", err)
		}
	})
	waitForFile(t, marker)
	if got, want := strings.TrimSpace(readFile(t, marker)), "--api-sock "+socket; got != want {
		t.Fatalf("VMM arguments = %q, want %q", got, want)
	}
	if err := vmm.Close(); err != nil {
		t.Fatal(err)
	}
	if err := vmm.Close(); err != nil {
		t.Fatalf("second Close = %v, want nil", err)
	}
	select {
	case <-vmm.Done():
	default:
		t.Fatal("VMM Done channel did not close after Close")
	}
	if err := vmm.ExitError(); err != nil {
		t.Fatalf("intentional SIGTERM exit error = %v, want nil", err)
	}
}

func TestStartVMMKillsAndReapsAfterStopGrace(t *testing.T) {
	dir := t.TempDir()
	marker := filepath.Join(dir, "started")
	binary := writeShell(t, dir, "stubborn-vmm", fmt.Sprintf("trap '' TERM\nprintf 'ready' > %s\nwhile :; do :; done\n", shellQuote(marker)))
	vmm, err := StartVMM(binary, filepath.Join(dir, "api.sock"), filepath.Join(dir, "firecracker.log"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := vmm.Close(); err != nil {
			t.Errorf("close VMM during cleanup: %v", err)
		}
	})
	waitForFile(t, marker)
	if err := vmm.closeAfter(10 * time.Millisecond); err != nil {
		t.Fatal(err)
	}
	select {
	case <-vmm.Done():
	default:
		t.Fatal("VMM Done channel did not close after SIGKILL")
	}
	var exitError *exec.ExitError
	if err := vmm.ExitError(); !errors.As(err, &exitError) {
		t.Fatalf("SIGKILL exit error = %v, want exec.ExitError", err)
	}
	if err := vmm.Close(); err != nil {
		t.Fatalf("Close after SIGKILL = %v, want nil", err)
	}
}

func TestVMMExitErrorReportsUnexpectedExit(t *testing.T) {
	dir := t.TempDir()
	binary := writeShell(t, dir, "exiting-vmm", "exit 17\n")
	vmm, err := StartVMM(binary, filepath.Join(dir, "api.sock"), filepath.Join(dir, "firecracker.log"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := vmm.Close(); err != nil {
			t.Errorf("close VMM during cleanup: %v", err)
		}
	})
	select {
	case <-vmm.Done():
	case <-time.After(3 * time.Second):
		t.Fatal("VMM did not exit")
	}
	var exitError *exec.ExitError
	if err := vmm.ExitError(); !errors.As(err, &exitError) || exitError.ExitCode() != 17 {
		t.Fatalf("ExitError = %v, want exit code 17", err)
	}
	if err := vmm.Close(); err != nil {
		t.Fatalf("Close after natural exit = %v, want nil", err)
	}
}

func writeShell(t *testing.T, dir, name, body string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	contents := "#!/bin/sh\n" + body
	if err := os.WriteFile(path, []byte(contents), 0o700); err != nil {
		t.Fatal(err)
	}
	return path
}

func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\\''") + "'"
}

func waitForFile(t *testing.T, path string) {
	t.Helper()
	timeout := time.NewTimer(3 * time.Second)
	defer timeout.Stop()
	ticker := time.NewTicker(5 * time.Millisecond)
	defer ticker.Stop()
	for {
		if data, err := os.ReadFile(path); err == nil && len(data) > 0 {
			return
		} else if err != nil && !errors.Is(err, os.ErrNotExist) {
			t.Fatal(err)
		}
		select {
		case <-ticker.C:
		case <-timeout.C:
			t.Fatalf("timed out waiting for %s", path)
		}
	}
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	contents, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(contents)
}
