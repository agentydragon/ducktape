package vsock

import (
	"bufio"
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestDialPreservesBytesAfterHandshake(t *testing.T) {
	socketPath := filepath.Join(t.TempDir(), "vsock.sock")
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()

	accepted := make(chan error, 1)
	go func() {
		connection, err := listener.Accept()
		if err != nil {
			accepted <- err
			return
		}
		defer connection.Close()
		request, err := bufio.NewReader(connection).ReadString('\n')
		if err != nil {
			accepted <- err
			return
		}
		if request != "CONNECT 1234\n" {
			accepted <- errors.New("unexpected vsock CONNECT request: " + request)
			return
		}
		_, err = io.WriteString(connection, "OK 9876\nHTTP/2 preface")
		accepted <- err
	}()

	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	connection, err := Dial(ctx, socketPath, 1234)
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
	if err := <-accepted; err != nil {
		t.Fatal(err)
	}
}

func TestDialHandshakeHonorsContextDeadline(t *testing.T) {
	socketPath := filepath.Join(t.TempDir(), "vsock.sock")
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()

	accepted := make(chan net.Conn, 1)
	go func() {
		connection, err := listener.Accept()
		if err != nil {
			accepted <- nil
			return
		}
		accepted <- connection
	}()

	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Millisecond)
	defer cancel()
	connection, err := Dial(ctx, socketPath, 1234)
	if err == nil {
		_ = connection.Close()
		t.Fatal("Dial succeeded without a handshake response")
	}
	if !errors.Is(err, context.DeadlineExceeded) && !errors.Is(err, net.ErrClosed) && !errors.Is(err, os.ErrDeadlineExceeded) {
		t.Fatalf("Dial error = %v, want a context deadline", err)
	}
	peer := <-accepted
	if peer == nil {
		t.Fatal("Firecracker socket did not accept the connection")
	}
	defer peer.Close()
	peerReader := bufio.NewReader(peer)
	request, err := peerReader.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	if request != "CONNECT 1234\n" {
		t.Fatalf("vsock request = %q", request)
	}
	_ = peer.SetReadDeadline(time.Now().Add(time.Second))
	if _, err := peerReader.ReadByte(); !errors.Is(err, io.EOF) {
		t.Fatalf("Firecracker-side read after the CONNECT line = %v, want EOF", err)
	}
}

func TestDialRejectsMalformedOKResponse(t *testing.T) {
	socketPath := filepath.Join(t.TempDir(), "vsock.sock")
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()

	accepted := make(chan error, 1)
	go func() {
		connection, err := listener.Accept()
		if err != nil {
			accepted <- err
			return
		}
		defer connection.Close()
		if _, err := bufio.NewReader(connection).ReadString('\n'); err != nil {
			accepted <- err
			return
		}
		_, err = io.WriteString(connection, "OK host\n")
		accepted <- err
	}()

	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	connection, err := Dial(ctx, socketPath, 1234)
	if err == nil {
		_ = connection.Close()
		t.Fatal("Dial accepted a non-decimal handshake port")
	}
	if err := <-accepted; err != nil {
		t.Fatal(err)
	}
}
