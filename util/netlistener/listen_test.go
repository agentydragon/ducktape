package netlistener

import (
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"strconv"
	"syscall"
	"testing"
	"time"
)

func TestOpenServesOnTheInheritedSocket(t *testing.T) {
	original, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = original.Close() })

	file, err := original.(*net.TCPListener).File()
	if err != nil {
		t.Fatal(err)
	}
	inheritedFD, err := syscall.Dup(int(file.Fd()))
	if err != nil {
		_ = file.Close()
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		_ = syscall.Close(inheritedFD)
		t.Fatal(err)
	}

	inherited, err := Open("fd:" + strconv.Itoa(inheritedFD))
	if err != nil {
		t.Fatal(err)
	}
	if got, want := inherited.Addr().String(), original.Addr().String(); got != want {
		t.Fatalf("inherited listener address %q, original held socket address %q", got, want)
	}

	if err := original.Close(); err != nil {
		t.Fatal(err)
	}

	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, "served by inherited listener")
	})}
	serveDone := make(chan error, 1)
	go func() { serveDone <- server.Serve(inherited) }()
	t.Cleanup(func() {
		_ = server.Close()
		select {
		case err := <-serveDone:
			if err != nil && !errors.Is(err, http.ErrServerClosed) && !errors.Is(err, net.ErrClosed) {
				t.Errorf("HTTP server stopped with an unexpected error: %v", err)
			}
		case <-time.After(time.Second):
			t.Error("HTTP server did not stop after closing its listener")
		}
	})

	client := http.Client{Timeout: 2 * time.Second}
	response, err := client.Get("http://" + inherited.Addr().String())
	if err != nil {
		t.Fatalf("request through inherited listener: %v", err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || string(body) != "served by inherited listener" {
		t.Fatalf("response = %d %q", response.StatusCode, body)
	}
}

func TestOpenRejectsInvalidInheritedDescriptors(t *testing.T) {
	for _, address := range []string{"fd:not-a-number", "fd:-1", "fd:0", "fd:1", "fd:2"} {
		t.Run(address, func(t *testing.T) {
			listener, err := Open(address)
			if err == nil {
				_ = listener.Close()
				t.Fatalf("Open(%q) unexpectedly succeeded", address)
			}
		})
	}
}

func TestOpenBindsTCPAddress(t *testing.T) {
	listener, err := Open("127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	if _, ok := listener.(*net.TCPListener); !ok {
		t.Fatalf("listener type = %T", listener)
	}
	other, err := net.Listen("tcp", listener.Addr().String())
	if err == nil {
		other.Close()
		t.Fatal("another listener acquired the bound address")
	}
}

func TestOpenRejectsNonListeningDescriptor(t *testing.T) {
	read, write, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer read.Close()
	defer write.Close()
	fd, err := syscall.Dup(int(read.Fd()))
	if err != nil {
		t.Fatal(err)
	}
	listener, err := Open("fd:" + strconv.Itoa(fd))
	if err == nil {
		listener.Close()
		t.Fatal("pipe accepted as a listener")
	}
}
