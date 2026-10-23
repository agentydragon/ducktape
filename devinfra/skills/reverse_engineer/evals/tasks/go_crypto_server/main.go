package main

import (
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"syscall"
)

// The eval is built as a standalone stdlib-only module, including its garbled variant.
// Keep the same address-or-fd startup contract as util/netlistener here.
func openListener(address string) (net.Listener, error) {
	fdText, inherited := strings.CutPrefix(address, "fd:")
	if !inherited {
		return net.Listen("tcp", address)
	}
	fd, err := strconv.Atoi(fdText)
	if err != nil || fd < 3 {
		return nil, fmt.Errorf("invalid inherited listener %q", address)
	}
	file := os.NewFile(uintptr(fd), address)
	defer file.Close()
	listening, err := syscall.GetsockoptInt(fd, syscall.SOL_SOCKET, syscall.SO_ACCEPTCONN)
	if err != nil {
		return nil, fmt.Errorf("inspect listener %q: %w", address, err)
	}
	if listening != 1 {
		return nil, fmt.Errorf("descriptor %q is not listening", address)
	}
	listener, err := net.FileListener(file)
	if err != nil {
		return nil, err
	}
	if _, ok := listener.(*net.TCPListener); !ok {
		listener.Close()
		return nil, fmt.Errorf("descriptor %q is not a TCP listener", address)
	}
	return listener, nil
}

func run(address string) error {
	listener, err := openListener(address)
	if err != nil {
		return err
	}
	defer listener.Close()
	fmt.Fprintf(os.Stderr, "ncs %s listening on %s\n", protocolVersion, listener.Addr())
	return http.Serve(listener, newServer().routes())
}

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "TCP address or inherited listening socket fd:N")
	flag.Parse()
	if err := run(*addr); err != nil {
		fmt.Fprintf(os.Stderr, "server error: %v\n", err)
		os.Exit(1)
	}
}
