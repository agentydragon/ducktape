package httpconnect

import (
	"bufio"
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestOpenPreservesBytesFollowingSuccessfulResponse(t *testing.T) {
	client, server := net.Pipe()
	defer server.Close()
	serverDone := make(chan error, 1)
	go func() {
		request, err := http.ReadRequest(bufio.NewReader(server))
		if err != nil {
			serverDone <- err
			return
		}
		if request.Method != http.MethodConnect || request.Host != "service.test:443" {
			serverDone <- errors.New("unexpected CONNECT request")
			_, _ = io.WriteString(server, "HTTP/1.1 500 Bad Request\r\nContent-Length: 0\r\n\r\n")
			return
		}
		if got := request.Header.Get("Proxy-Authorization"); got != "Bearer test-token" {
			serverDone <- errors.New("CONNECT request did not preserve supplied headers")
			_, _ = io.WriteString(server, "HTTP/1.1 500 Bad Request\r\nContent-Length: 0\r\n\r\n")
			return
		}
		_, err = io.WriteString(server, "HTTP/1.1 200 Connection Established\r\n\r\nfirst tunnel bytes")
		serverDone <- err
	}()

	ctx, cancel := context.WithCancel(context.Background())
	tunnel, err := Open(ctx, client, "service.test:443", http.Header{"Proxy-Authorization": {"Bearer test-token"}})
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	defer tunnel.Close()
	if err := <-serverDone; err != nil {
		cancel()
		t.Fatal(err)
	}
	got := make([]byte, len("first tunnel bytes"))
	if _, err := io.ReadFull(tunnel, got); err != nil {
		cancel()
		t.Fatal(err)
	}
	if string(got) != "first tunnel bytes" {
		t.Fatalf("tunnel payload = %q", got)
	}
	cancel()
}

func TestOpenClosesConnectionWhenProxyRefuses(t *testing.T) {
	client, server := net.Pipe()
	defer server.Close()
	go func() {
		_, _ = http.ReadRequest(bufio.NewReader(server))
		_, _ = io.WriteString(server, "HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
	}()

	if _, err := Open(context.Background(), client, "service.test:443", nil); err == nil || !strings.Contains(err.Error(), "407 Proxy Authentication Required") {
		t.Fatalf("Open refusal error = %v", err)
	}
	if _, err := server.Read(make([]byte, 1)); err == nil {
		t.Fatal("proxy connection remained open after refusal")
	}
}

func TestOpenCancellationInterruptsResponseReadAndClosesConnection(t *testing.T) {
	client, server := net.Pipe()
	defer server.Close()
	requestRead := make(chan error, 1)
	go func() {
		_, err := http.ReadRequest(bufio.NewReader(server))
		requestRead <- err
	}()

	ctx, cancel := context.WithCancel(context.Background())
	result := make(chan error, 1)
	go func() {
		_, err := Open(ctx, client, "service.test:443", nil)
		result <- err
	}()
	if err := <-requestRead; err != nil {
		cancel()
		t.Fatalf("read CONNECT request: %v", err)
	}
	cancel()
	select {
	case err := <-result:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("Open cancellation error = %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("Open did not stop after its context was canceled")
	}
}
