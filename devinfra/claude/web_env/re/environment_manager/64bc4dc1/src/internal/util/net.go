// Reconstructed from a6f96673 DWARF extraction, carried forward to 64bc4dc1.
// Source: internal/util/net.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/util/net.go
//
// This file exists in the old binary's DWARF paths but has no TEXT symbols
// (all functions were inlined by the compiler). The file contains network
// utility functions used by MCP servers and other components that need to
// find available ports.

package util

import (
	"fmt"
	"net"
)

// GetFreePort finds an available TCP port by listening on :0 and returning
// the OS-assigned port number. The listener is immediately closed.
//
// Used by MCP server Start methods to find available ports for HTTP listeners.
// All call sites were inlined in the old binary (no TEXT entry), but the
// source path is present in DWARF info.
func GetFreePort() (int, error) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, fmt.Errorf("failed to find free port: %w", err)
	}
	defer listener.Close()

	addr := listener.Addr().(*net.TCPAddr)
	return addr.Port, nil
}
