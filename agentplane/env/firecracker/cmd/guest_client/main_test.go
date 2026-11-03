package main

import (
	"bufio"
	"bytes"
	"context"
	"io"
	"net"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestDialVsockConsumesOnlyTheLauncherHandshake(t *testing.T) {
	socketPath := filepath.Join(t.TempDir(), "vsock.sock")
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()

	type result struct {
		request string
		err     error
	}
	accepted := make(chan result, 1)
	go func() {
		connection, err := listener.Accept()
		if err != nil {
			accepted <- result{err: err}
			return
		}
		defer connection.Close()
		request, err := bufio.NewReader(connection).ReadString('\n')
		if err != nil {
			accepted <- result{err: err}
			return
		}
		if _, err := io.WriteString(connection, "OK 52\nHTTP/2 preface"); err != nil {
			accepted <- result{request: request, err: err}
			return
		}
		accepted <- result{request: request}
	}()

	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	connection, err := dialVsock(ctx, socketPath, 52)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	preface := make([]byte, len("HTTP/2 preface"))
	if _, err := io.ReadFull(connection, preface); err != nil {
		t.Fatal(err)
	}
	if string(preface) != "HTTP/2 preface" {
		t.Fatalf("post-handshake bytes = %q, want %q", preface, "HTTP/2 preface")
	}
	if result := <-accepted; result.err != nil {
		t.Fatal(result.err)
	} else if result.request != "CONNECT 52\n" {
		t.Fatalf("launcher request = %q, want %q", result.request, "CONNECT 52\n")
	}
}

func TestReadSynchronizeRequestBoundsAndDecodesProtobuf(t *testing.T) {
	encoded := []byte{0x12, 0x03, 'c', 'e', 'r'}
	request, err := readSynchronizeRequest(bytes.NewReader(encoded))
	if err != nil {
		t.Fatal(err)
	}
	if request.GetTlsCert() != "cer" {
		t.Fatalf("TLS certificate = %q, want cer", request.GetTlsCert())
	}
	if _, err := readSynchronizeRequest(strings.NewReader(strings.Repeat("x", maxRequestBytes+1))); err == nil {
		t.Fatal("accepted oversized SynchronizeRequest input")
	}
}

func TestRunHelpExitsSuccessfully(t *testing.T) {
	if err := run([]string{"--help"}, strings.NewReader(""), io.Discard); err != nil {
		t.Fatalf("run --help: %v", err)
	}
}

func TestRunRejectsPositionalArguments(t *testing.T) {
	err := run([]string{"--socket", "/tmp/vsock.sock", "extra"}, strings.NewReader(""), io.Discard)
	if err == nil || !strings.Contains(err.Error(), "unexpected positional arguments") {
		t.Fatalf("run with positional argument error = %v, want rejection", err)
	}
}
