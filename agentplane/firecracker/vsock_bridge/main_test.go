package main

import (
	"strings"
	"testing"
)

func TestRunHelpExitsSuccessfully(t *testing.T) {
	if err := run([]string{"--help"}); err != nil {
		t.Fatalf("run --help: %v", err)
	}
}

func TestRunRejectsInvalidArguments(t *testing.T) {
	if err := run([]string{"--socket", "/tmp/vsock.sock", "--listen", "/tmp/grpc.sock", "extra"}); err == nil || !strings.Contains(err.Error(), "unexpected positional arguments") {
		t.Fatalf("run with positional argument error = %v, want rejection", err)
	}
	if err := run([]string{"--socket", "/tmp/vsock.sock"}); err == nil || !strings.Contains(err.Error(), "--listen is required") {
		t.Fatalf("run without --listen error = %v, want rejection", err)
	}
	if err := run([]string{"--listen", "/tmp/grpc.sock"}); err == nil || !strings.Contains(err.Error(), "--socket is required") {
		t.Fatalf("run without --socket error = %v, want rejection", err)
	}
}
