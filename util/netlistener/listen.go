// Package netlistener acquires owned TCP listeners from addresses or inherited descriptors.
package netlistener

import (
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"syscall"
)

// Open binds a TCP address or consumes an inherited listening TCP descriptor (fd:N).
func Open(address string) (net.Listener, error) {
	fdText, inherited := strings.CutPrefix(address, "fd:")
	if !inherited {
		return net.Listen("tcp", address)
	}
	fd, err := strconv.Atoi(fdText)
	if err != nil || fd < 3 {
		return nil, fmt.Errorf("invalid inherited listener address %q", address)
	}
	file := os.NewFile(uintptr(fd), address)
	if file == nil {
		return nil, fmt.Errorf("invalid inherited listener file descriptor %d", fd)
	}
	listening, err := syscall.GetsockoptInt(fd, syscall.SOL_SOCKET, syscall.SO_ACCEPTCONN)
	if err != nil {
		file.Close()
		return nil, fmt.Errorf("inspect inherited listener %q: %w", address, err)
	}
	if listening != 1 {
		file.Close()
		return nil, fmt.Errorf("inherited descriptor %q is not listening", address)
	}
	listener, err := net.FileListener(file)
	closeErr := file.Close()
	if err != nil {
		return nil, fmt.Errorf("open inherited listener %q: %w", address, err)
	}
	if closeErr != nil {
		listener.Close()
		return nil, fmt.Errorf("close inherited listener file %q: %w", address, closeErr)
	}
	if _, ok := listener.(*net.TCPListener); !ok {
		listener.Close()
		return nil, fmt.Errorf("inherited descriptor %q is not a TCP listener", address)
	}
	return listener, nil
}
