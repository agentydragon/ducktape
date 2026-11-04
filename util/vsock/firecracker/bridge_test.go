package firecracker

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
	_ = peer.SetReadDeadline(time.Now().Add(time.Second))
	if _, err := peer.Read(make([]byte, 1)); err == nil {
		t.Fatal("Dial left its connection open after the context expired")
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

func TestServeCopiesDuplexBytesAndCleansUp(t *testing.T) {
	directory := t.TempDir()
	vsockPath := filepath.Join(directory, "vsock.sock")
	listenPath := filepath.Join(directory, "grpc.sock")
	vsockListener, err := net.Listen("unix", vsockPath)
	if err != nil {
		t.Fatal(err)
	}
	defer vsockListener.Close()

	guestResult := make(chan error, 1)
	go func() {
		connection, err := vsockListener.Accept()
		if err != nil {
			guestResult <- err
			return
		}
		defer connection.Close()
		request, err := bufio.NewReader(connection).ReadString('\n')
		if err != nil {
			guestResult <- err
			return
		}
		if request != "CONNECT 1234\n" {
			guestResult <- errors.New("unexpected vsock CONNECT request: " + request)
			return
		}
		if _, err := io.WriteString(connection, "OK 9876\nwelcome"); err != nil {
			guestResult <- err
			return
		}
		requestBytes, err := io.ReadAll(connection)
		if err != nil {
			guestResult <- err
			return
		}
		if string(requestBytes) != "grpc request" {
			guestResult <- errors.New("unexpected bridged request bytes: " + string(requestBytes))
			return
		}
		if _, err := io.WriteString(connection, " reply"); err != nil {
			guestResult <- err
			return
		}
		if closeWriter, ok := connection.(interface{ CloseWrite() error }); ok {
			if err := closeWriter.CloseWrite(); err != nil {
				guestResult <- err
				return
			}
		}
		guestResult <- nil
	}()

	localListener, err := net.Listen("unix", listenPath)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	serveResult := make(chan error, 1)
	go func() { serveResult <- Serve(ctx, localListener, vsockPath, 1234, time.Second) }()
	client, err := waitForUnixSocket(listenPath)
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	defer client.Close()
	_ = client.SetDeadline(time.Now().Add(2 * time.Second))
	if _, err := io.WriteString(client, "grpc request"); err != nil {
		t.Fatal(err)
	}
	if closeWriter, ok := client.(interface{ CloseWrite() error }); ok {
		if err := closeWriter.CloseWrite(); err != nil {
			t.Fatal(err)
		}
	}
	response, err := io.ReadAll(client)
	if err != nil {
		t.Fatal(err)
	}
	if string(response) != "welcome reply" {
		t.Fatalf("bridged response = %q, want %q", response, "welcome reply")
	}
	if err := <-guestResult; err != nil {
		t.Fatal(err)
	}

	cancel()
	if err := waitForServeResult(t, serveResult); err != nil {
		t.Fatalf("serve after cancellation: %v", err)
	}
	if _, err := net.Dial("unix", listenPath); err == nil {
		t.Fatal("local socket remained connectable after shutdown")
	}
}

func TestServeKeepsAcceptingAfterGuestBootRejection(t *testing.T) {
	directory := t.TempDir()
	vsockPath := filepath.Join(directory, "vsock.sock")
	listenPath := filepath.Join(directory, "grpc.sock")
	vsockListener, err := net.Listen("unix", vsockPath)
	if err != nil {
		t.Fatal(err)
	}
	defer vsockListener.Close()

	guestResult := make(chan error, 1)
	go func() {
		first, err := acceptVsockConnect(vsockListener)
		if err != nil {
			guestResult <- err
			return
		}
		_, err = io.WriteString(first, "ERR guest is still booting\n")
		_ = first.Close()
		if err != nil {
			guestResult <- err
			return
		}

		second, err := acceptVsockConnect(vsockListener)
		if err != nil {
			guestResult <- err
			return
		}
		defer second.Close()
		if _, err := io.WriteString(second, "OK 9876\n"); err != nil {
			guestResult <- err
			return
		}
		request, err := io.ReadAll(second)
		if err != nil {
			guestResult <- err
			return
		}
		if _, err := io.WriteString(second, "ready:"+string(request)); err != nil {
			guestResult <- err
			return
		}
		if closeWriter, ok := second.(interface{ CloseWrite() error }); ok {
			if err := closeWriter.CloseWrite(); err != nil {
				guestResult <- err
				return
			}
		}
		guestResult <- nil
	}()

	localListener, err := net.Listen("unix", listenPath)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	serveResult := make(chan error, 1)
	go func() { serveResult <- Serve(ctx, localListener, vsockPath, 1234, time.Second) }()
	first, err := waitForUnixSocket(listenPath)
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	_ = first.SetDeadline(time.Now().Add(2 * time.Second))
	_, _ = io.Copy(io.Discard, first)
	_ = first.Close()
	second, err := net.Dial("unix", listenPath)
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	defer second.Close()
	_ = second.SetDeadline(time.Now().Add(2 * time.Second))
	if _, err := io.WriteString(second, "next attempt"); err != nil {
		t.Fatal(err)
	}
	if closeWriter, ok := second.(interface{ CloseWrite() error }); ok {
		if err := closeWriter.CloseWrite(); err != nil {
			t.Fatal(err)
		}
	}
	response, err := io.ReadAll(second)
	if err != nil {
		t.Fatal(err)
	}
	if string(response) != "ready:next attempt" {
		t.Fatalf("response after guest boot rejection = %q", response)
	}
	if err := <-guestResult; err != nil {
		t.Fatal(err)
	}
	cancel()
	if err := waitForServeResult(t, serveResult); err != nil {
		t.Fatalf("serve after cancellation: %v", err)
	}
}

func TestServeCancellationClosesPendingHandshake(t *testing.T) {
	directory := t.TempDir()
	vsockPath := filepath.Join(directory, "vsock.sock")
	listenPath := filepath.Join(directory, "grpc.sock")
	vsockListener, err := net.Listen("unix", vsockPath)
	if err != nil {
		t.Fatal(err)
	}
	defer vsockListener.Close()

	accepted := make(chan net.Conn, 1)
	go func() {
		connection, err := vsockListener.Accept()
		if err == nil {
			accepted <- connection
			return
		}
		accepted <- nil
	}()
	localListener, err := net.Listen("unix", listenPath)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	serveResult := make(chan error, 1)
	go func() { serveResult <- Serve(ctx, localListener, vsockPath, 1234, time.Second) }()
	client, err := waitForUnixSocket(listenPath)
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	defer client.Close()
	guest := <-accepted
	if guest == nil {
		t.Fatal("bridge did not connect to the Firecracker socket")
	}
	defer guest.Close()
	request, err := bufio.NewReader(guest).ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	if request != "CONNECT 1234\n" {
		t.Fatalf("vsock request = %q", request)
	}

	cancel()
	if err := waitForServeResult(t, serveResult); err != nil {
		t.Fatalf("serve after cancellation: %v", err)
	}
	_ = client.SetReadDeadline(time.Now().Add(time.Second))
	if _, err := client.Read(make([]byte, 1)); !errors.Is(err, io.EOF) {
		t.Fatalf("local connection after cancellation error = %v, want EOF", err)
	}
}

func acceptVsockConnect(listener net.Listener) (net.Conn, error) {
	connection, err := listener.Accept()
	if err != nil {
		return nil, err
	}
	request, err := bufio.NewReader(connection).ReadString('\n')
	if err != nil {
		_ = connection.Close()
		return nil, err
	}
	if request != "CONNECT 1234\n" {
		_ = connection.Close()
		return nil, errors.New("unexpected vsock CONNECT request: " + request)
	}
	return connection, nil
}

func waitForUnixSocket(path string) (net.Conn, error) {
	ticker := time.NewTicker(5 * time.Millisecond)
	defer ticker.Stop()
	timer := time.NewTimer(time.Second)
	defer timer.Stop()
	for {
		connection, err := net.Dial("unix", path)
		if err == nil {
			return connection, nil
		}
		select {
		case <-ticker.C:
		case <-timer.C:
			return nil, errors.New("local gRPC socket did not become available")
		}
	}
}

func waitForServeResult(t *testing.T, result <-chan error) error {
	t.Helper()
	select {
	case err := <-result:
		return err
	case <-time.After(time.Second):
		t.Fatal("bridge did not stop before the test deadline")
		return nil
	}
}
