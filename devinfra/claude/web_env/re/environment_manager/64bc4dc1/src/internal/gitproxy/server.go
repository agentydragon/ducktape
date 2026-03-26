// Reconstructed from a6f96673 DWARF extraction, carried forward to 64bc4dc1.
// Package: internal/gitproxy
// Source: internal/gitproxy/server.go

package gitproxy

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"sync"
	"time"
)

// RepoAuth holds authentication configuration for a single git repository.
//
// Struct layout (from type equality function at 0xaea320):
//
//	offset 0x00: Repo string (ptr + len)
//	offset 0x10: Token string (ptr + len)
//	offset 0x20: UpstreamURL string (ptr + len)
type RepoAuth struct {
	Repo        string // offset 0x00: repository identifier (e.g., "owner/repo")
	Token       string // offset 0x10: authentication token
	UpstreamURL string // offset 0x20: upstream git server URL
}

// ServerConfig holds configuration for creating a git proxy server.
//
// Struct layout (from NewServer field access patterns):
//
//	offset 0x00: SessionID string (ptr + len) - checked at offset 0x08 for len
//	offset 0x10: SessionIngressURL string (ptr + len) - checked at offset 0x18 for len
//	offset 0x20: RepoAuths []RepoAuth (ptr + len + cap) - iterated at offset 0x28 for len
//	offset 0x38: MaxRetries int
//	offset 0x40: Logger *slog.Logger
type ServerConfig struct {
	SessionID         string       // offset 0x00
	SessionIngressURL string       // offset 0x10
	RepoAuths         []RepoAuth   // offset 0x20
	MaxRetries        int          // offset 0x38
	Logger            *slog.Logger // offset 0x40
}

// Server is the interface for the git proxy HTTP server.
//
// Methods (from itab at 0xf65948):
//   - Start(ctx context.Context, logger *slog.Logger) error
//   - Stop(ctx context.Context, logger *slog.Logger) error
//   - Port() int
//   - BaseURL() string
type Server interface {
	Start(ctx context.Context, logger *slog.Logger) error
	Stop(ctx context.Context, logger *slog.Logger) error
	Port() int
	BaseURL() string
}

// server is the concrete implementation of Server.
//
// Struct layout (from type equality at 0xaea240 and field accesses):
//
//	offset 0x00: config *ServerConfig
//	offset 0x08: httpServer *http.Server
//	offset 0x10: listener net.Listener
//	offset 0x18: port int
//	offset 0x20: logger *slog.Logger
//	offset 0x28: mu sync.RWMutex (embedded, size 0x18)
//	offset 0x40: started bool
//
// Implements: Server (itab at 0xf65948)
type server struct {
	config            *ServerConfig       // offset 0x00
	httpServer        *http.Server        // offset 0x08
	listener          net.Listener        // offset 0x10 (interface: itab + data)
	port              int                 // offset 0x18 (not used directly; derived from listener addr)
	logger            *slog.Logger        // offset 0x20
	mu                sync.RWMutex        // offset 0x28 (size 0x18)
	started           bool                // offset 0x40
	sessionIngressURL string              // accessed via config
	sessionID         string              // accessed via config
	repoAuths         map[string]RepoAuth // built from config.RepoAuths
}

// NewServer creates a new git proxy server from the given config.
// It validates the config and builds the repo auth lookup map.
//
// Binary address: 0xae8b80
// Source file: server.go
func NewServer(config *ServerConfig) (*server, error) {
	if config == nil {
		return nil, fmt.Errorf("config is required")
	}
	if config.SessionID == "" {
		return nil, fmt.Errorf("session ingress URL is required")
	}
	if config.SessionIngressURL == "" {
		return nil, fmt.Errorf("session ID is required")
	}

	// Validate each RepoAuth
	for i, auth := range config.RepoAuths {
		if auth.Repo == "" {
			return nil, fmt.Errorf("repo is required for RepoAuth at index %d", i)
		}
		if auth.Token == "" {
			return nil, fmt.Errorf("auth token is required for repo %s", auth.Repo)
		}
	}

	// Verify at least one repo auth is configured
	if len(config.RepoAuths) == 0 {
		return nil, fmt.Errorf("at least one repository auth is required")
	}

	s := &server{
		config: config,
		logger: config.Logger,
	}

	// Build the repoAuths map
	s.repoAuths = make(map[string]RepoAuth)
	for _, auth := range config.RepoAuths {
		s.repoAuths[auth.Repo] = auth
	}
	s.sessionIngressURL = config.SessionIngressURL
	s.sessionID = config.SessionID

	return s, nil
}

// Start begins listening and serving HTTP requests for the git proxy.
// It attempts to listen on "127.0.0.1:{port}" and retries up to MaxRetries
// times with exponential backoff (10 seconds between retries) if the port
// is in use.
//
// Binary address: 0xae8dc0
// Source file: server.go
//
// Closures:
//
//	func1 at 0xae9a20 - goroutine running http.Server.Serve
//	deferwrap1 at 0xae9be0 - deferred RWMutex unlock
//	newHandler.func2 at 0xaea220 - handler factory
func (s *server) Start(ctx context.Context, logger *slog.Logger) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.started {
		return fmt.Errorf("server already started")
	}

	// Format the listen address
	addr := fmt.Sprintf("127.0.0.1:%d", s.config.MaxRetries)

	var err error
	retries := 0
	for retries <= 2 {
		if retries > 0 {
			// Calculate backoff: retries * 10 seconds
			backoffSeconds := retries * 10
			backoffDuration := time.Duration(backoffSeconds) * time.Second

			logger.Warn("Retrying git proxy server start after failure",
				"error", err,
				"attempt", retries,
				"backoff_seconds", backoffSeconds,
			)

			// Wait with context cancellation support
			timer := time.NewTimer(backoffDuration)
			select {
			case <-ctx.Done():
				timer.Stop()
				return ctx.Err()
			case <-timer.C:
			}
		}

		// Attempt to listen
		var listener net.Listener
		listener, err = net.Listen("tcp", addr)
		if err != nil {
			if retries >= 2 {
				// Log the final failure
				logger.Error("failed to start git proxy server",
					"error", err,
					"attempt", retries,
				)
				return err
			}
			retries++
			continue
		}

		// Success - store the listener and create the HTTP server
		s.listener = listener
		s.started = true

		// Create the HTTP handler
		h := &handler{
			server:     s,
			httpClient: &http.Client{},
			logger:     logger,
		}

		s.httpServer = &http.Server{
			Handler: h,
		}

		logger.Info("Git proxy server listening",
			"address", listener.Addr().String(),
		)

		// Start serving in a goroutine
		// func1 at 0xae9a20
		go func() {
			if err := s.httpServer.Serve(listener); err != nil && err != http.ErrServerClosed {
				logger.Error("git proxy server error",
					"error", err,
				)
			}
		}()

		return nil
	}

	return err
}

// Stop gracefully shuts down the git proxy server.
// It first attempts a graceful shutdown with a 10-second timeout,
// and if that fails, does a hard close.
//
// Binary address: 0xae9c40
// Source file: server.go
//
// Closure:
//
//	deferwrap1 at 0xae9fc0 - deferred RWMutex unlock
func (s *server) Stop(ctx context.Context, logger *slog.Logger) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if !s.started {
		return nil
	}

	logger.Info("Stopping local git proxy server")

	if s.httpServer == nil {
		s.started = false
		return nil
	}

	// Attempt graceful shutdown with 10-second timeout
	shutdownCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()

	err := s.httpServer.Shutdown(shutdownCtx)
	if err != nil {
		logger.Error("Error during server shutdown",
			"error", err,
		)

		// Fall back to hard close
		closeErr := s.httpServer.Close()
		if closeErr != nil {
			return fmt.Errorf("failed to close server: %w", closeErr)
		}
	}

	s.started = false
	return nil
}

// Port returns the port number the server is listening on.
// Returns 0 if the server is not started or has no listener.
//
// Binary address: 0xaea020
// Source file: server.go
//
// Closure:
//
//	deferwrap1 at 0xaea140 - deferred RWMutex read-unlock
func (s *server) Port() int {
	s.mu.RLock()
	defer s.mu.RUnlock()

	if s.listener == nil {
		return 0
	}

	addr := s.listener.Addr()
	tcpAddr := addr.(*net.TCPAddr)
	return tcpAddr.Port
}

// BaseURL returns the base URL for the git proxy server (e.g., "http://127.0.0.1:12345").
//
// Binary address: 0xaea1a0
// Source file: server.go
func (s *server) BaseURL() string {
	return fmt.Sprintf("http://127.0.0.1:%d", s.Port())
}
