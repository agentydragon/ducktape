// Package nettest supplies Linux TCP endpoints for tests of network failure.
package nettest

import (
	"net"
	"os"
	"strconv"
	"syscall"
	"testing"
)

// RefusingAddress holds an exclusive bound socket without listening until cleanup.
func RefusingAddress(t testing.TB) string {
	t.Helper()
	fd, err := syscall.Socket(syscall.AF_INET, syscall.SOCK_STREAM|syscall.SOCK_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	file := os.NewFile(uintptr(fd), "test-refusing-tcp")
	t.Cleanup(func() { file.Close() })
	if err := syscall.Bind(fd, &syscall.SockaddrInet4{Addr: [4]byte{127, 0, 0, 1}}); err != nil {
		t.Fatal(err)
	}
	address, err := syscall.Getsockname(fd)
	if err != nil {
		t.Fatal(err)
	}
	return net.JoinHostPort("127.0.0.1", strconv.Itoa(address.(*syscall.SockaddrInet4).Port))
}
