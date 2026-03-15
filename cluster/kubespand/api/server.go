package api

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"

	v1alpha1 "github.com/cosi-project/runtime/api/v1alpha1"
	"github.com/cosi-project/runtime/pkg/state"
	"go.uber.org/zap"
	"google.golang.org/grpc"
)

// Server exposes kubespand's COSI state via gRPC on a Unix socket.
type Server struct {
	grpcServer *grpc.Server
	socketPath string
	logger     *zap.Logger
}

// NewServer creates a gRPC server that exposes COSI state as read-only.
func NewServer(st state.CoreState, socketPath string, logger *zap.Logger) *Server {
	srv := grpc.NewServer()
	v1alpha1.RegisterStateServer(srv, NewReadOnlyState(st))

	return &Server{
		grpcServer: srv,
		socketPath: socketPath,
		logger:     logger,
	}
}

// Run starts the gRPC server on the configured Unix socket.
// Blocks until ctx is cancelled, then performs graceful shutdown.
func (s *Server) Run(ctx context.Context) error {
	dir := filepath.Dir(s.socketPath)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("creating socket directory %s: %w", dir, err)
	}

	// Remove stale socket from unclean shutdown.
	if err := os.Remove(s.socketPath); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("removing stale socket %s: %w", s.socketPath, err)
	}

	lis, err := net.Listen("unix", s.socketPath)
	if err != nil {
		return fmt.Errorf("listening on %s: %w", s.socketPath, err)
	}

	if err := os.Chmod(s.socketPath, 0o600); err != nil {
		lis.Close()
		return fmt.Errorf("setting socket permissions on %s: %w", s.socketPath, err)
	}

	s.logger.Info("API server listening", zap.String("socket", s.socketPath))

	go func() {
		<-ctx.Done()
		s.logger.Info("API server shutting down")
		s.grpcServer.GracefulStop()
	}()

	if err := s.grpcServer.Serve(lis); err != nil {
		return fmt.Errorf("serving gRPC: %w", err)
	}

	// Clean up socket file after shutdown.
	os.Remove(s.socketPath)

	return nil
}
