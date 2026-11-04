package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"time"

	runtime "github.com/agentydragon/ducktape/agentplane/env/runtime"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// This file replaces new_runtime.go only in the system-test executable. The gate holds one
// authenticated command before Runtime admission without holding control/Session locks or Quiesce.
func newRuntime(config runtime.Config) (*runtime.Runtime, error) {
	directory := filepath.Join(config.StateDir, "deletion-test-admission")
	if err := os.MkdirAll(directory, 0700); err != nil {
		return nil, err
	}
	config.ControlInterceptor = func(ctx context.Context, request any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		command, ok := request.(*environmentruntimepb.InterruptTurnRequest)
		if !ok {
			return handler(ctx, request)
		}
		armed, err := os.ReadFile(filepath.Join(directory, "command"))
		if errors.Is(err, os.ErrNotExist) {
			return handler(ctx, request)
		}
		if err != nil {
			return nil, status.Error(codes.Internal, err.Error())
		}
		if string(armed) != command.CommandId {
			return handler(ctx, request)
		}
		if err := os.WriteFile(filepath.Join(directory, "entered"), []byte(command.CommandId), 0600); err != nil {
			return nil, status.Error(codes.Internal, err.Error())
		}
		bounded, cancel := context.WithTimeout(ctx, 120*time.Second)
		defer cancel()
		ticker := time.NewTicker(10 * time.Millisecond)
		defer ticker.Stop()
		for {
			if _, err := os.Stat(filepath.Join(directory, "release")); err == nil {
				response, result := handler(ctx, request)
				// Persist the actual handler result so a transport rejection cannot satisfy the test.
				if err := os.WriteFile(filepath.Join(directory, "result"), []byte(status.Code(result).String()), 0600); err != nil {
					return nil, status.Error(codes.Internal, err.Error())
				}
				return response, result
			} else if !errors.Is(err, os.ErrNotExist) {
				return nil, status.Error(codes.Internal, err.Error())
			}
			select {
			case <-bounded.Done():
				return nil, status.FromContextError(bounded.Err()).Err()
			case <-ticker.C:
			}
		}
	}
	return runtime.New(config)
}
