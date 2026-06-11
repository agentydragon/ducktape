// Reconstructed from a6f96673 DWARF extraction, carried forward to 495ea204.
// Package: internal/gitproxy
// Source: internal/gitproxy/manager.go

package gitproxy

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"sync"
)

// Manager is the interface for managing the git proxy lifecycle.
//
// Methods (from itab at 0xf65980):
//   - Start(ctx context.Context, logger *slog.Logger) error
//   - Stop(ctx context.Context, logger *slog.Logger) error
//   - GetProxyURL(ctx context.Context, logger *slog.Logger) (string, error)
//   - IsRunning() bool
type Manager interface {
	Start(ctx context.Context, logger *slog.Logger) error
	Stop(ctx context.Context, logger *slog.Logger) error
	GetProxyURL(ctx context.Context, logger *slog.Logger) (string, error)
	IsRunning() bool
}

// manager is the concrete implementation of Manager.
//
// Struct layout (from type equality at 0xaea3a0 and field accesses):
//
//	offset 0x00: server Server (interface: itab + data)
//	offset 0x10: config *ServerConfig
//	offset 0x18: logger *slog.Logger
//	offset 0x20: mu sync.RWMutex (embedded, size 0x14 for the fields + alignment)
//	offset 0x38: running bool
//
// Implements: Manager (itab at 0xf65980)
type manager struct {
	server  Server        // offset 0x00 (interface)
	config  *ServerConfig // offset 0x10
	logger  *slog.Logger  // offset 0x18
	mu      sync.RWMutex  // offset 0x20
	running bool          // offset 0x38
}

// NewManager creates a new Manager that wraps a Server instance.
// It validates the config and creates the underlying server.
//
// Binary address: 0xae8060
// Source file: manager.go
func NewManager(config *ServerConfig) (Manager, error) {
	if config == nil {
		return nil, fmt.Errorf("config is required")
	}

	srv, err := NewServer(config)
	if err != nil {
		return nil, fmt.Errorf("failed to create server: %w", err)
	}

	return &manager{
		server: srv,
		config: config,
		logger: config.Logger,
	}, nil
}

// Start starts the git proxy server.
// It acquires a write lock, logs the start, delegates to the underlying
// server's Start method, and marks the manager as running on success.
//
// Binary address: 0xae81a0
// Source file: manager.go
//
// Closure:
//
//	deferwrap1 at 0xae8560 - deferred RWMutex unlock
func (m *manager) Start(ctx context.Context, logger *slog.Logger) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if m.running {
		return nil
	}

	logger.Info("Starting git proxy manager")

	err := m.server.Start(ctx, logger)
	if err != nil {
		return fmt.Errorf("failed to start server: %w", err)
	}

	m.running = true

	// Get the port for logging
	port := m.server.Port()
	logger.Info("Starting local git proxy",
		"port", port,
	)

	return nil
}

// Stop stops the git proxy server.
// It acquires a write lock, logs the stop, delegates to the underlying
// server's Stop method, and marks the manager as not running.
//
// Binary address: 0xae85c0
// Source file: manager.go
//
// Closure:
//
//	deferwrap1 at 0xae8800 - deferred RWMutex unlock
func (m *manager) Stop(ctx context.Context, logger *slog.Logger) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if !m.running {
		return nil
	}

	logger.Info("Stopping git proxy manager")

	err := m.server.Stop(ctx, logger)
	if err != nil {
		return fmt.Errorf("failed to stop server: %w", err)
	}

	m.running = false

	logger.Info("Git proxy manager stopped")

	return nil
}

// GetProxyURL returns the proxy URL for the git proxy server.
// The URL has the format "http://local_proxy@127.0.0.1:{port}/git/{path}".
// It replaces "http://" with "http://local_proxy@" for git credential helper
// identification, then prepends "/git/" as the path prefix.
//
// Binary address: 0xae8860
// Source file: manager.go
//
// Closure:
//
//	deferwrap1 at 0xae8a00 - deferred RWMutex read-unlock
func (m *manager) GetProxyURL(ctx context.Context, logger *slog.Logger) (string, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()

	if !m.running {
		return "", nil
	}

	baseURL := m.server.BaseURL()

	// Replace "http://" with "http://local_proxy@" for git credential identification
	proxyURL := strings.Replace(baseURL, "http://", "http://local_proxy@", 1)

	// Append the /git/ path prefix and the context path
	return "/git/" + proxyURL, nil
}

// IsRunning returns whether the git proxy manager is currently running.
//
// Binary address: 0xae8a60
// Source file: manager.go
//
// Closure:
//
//	deferwrap1 at 0xae8b20 - deferred RWMutex read-unlock
func (m *manager) IsRunning() bool {
	m.mu.RLock()
	defer m.mu.RUnlock()

	return m.running
}
