// Apid subprocess management for kubespand.
//
// Before starting apid, creates the filtered COSI state socket at
// constants.APIRuntimeSocketPath — the same socket that Talos's machined
// PreFunc creates (internal/app/machined/pkg/system/services/apid.go).
// Uses state.Filter from the COSI runtime (same primitive Talos uses) to
// restrict apid's view to secrets.API, network.NodeAddress, and
// network.HostnameStatus.
//
// Ref: internal/app/machined/pkg/system/services/apid.go (PreFunc + apidResourceFilter)
// Ref: pkg/machinery/resources/secrets/condition.go (APIReadyCondition)
package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"

	v1alpha1 "github.com/cosi-project/runtime/api/v1alpha1"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/protobuf/server"
	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/siderolabs/talos/pkg/machinery/resources/network"
	"github.com/siderolabs/talos/pkg/machinery/resources/secrets"
	"go.uber.org/zap"
	"google.golang.org/grpc"
)

// apidResourceFilter filters access to COSI state for apid.
// Copied from internal/app/machined/pkg/system/services/apid.go:57-74.
func apidResourceFilter(_ context.Context, access state.Access) error {
	if !access.Verb.Readonly() {
		return errors.New("write access denied")
	}

	switch {
	case access.ResourceNamespace == secrets.NamespaceName && access.ResourceType == secrets.APIType && access.ResourceID == secrets.APIID:
		// allowed, contains apid certificates
	case access.ResourceNamespace == network.NamespaceName && access.ResourceType == network.NodeAddressType:
		// allowed, contains local node addresses
	case access.ResourceNamespace == network.NamespaceName && access.ResourceType == network.HostnameStatusType:
		// allowed, contains local node hostname
	default:
		return errors.New("access denied")
	}

	return nil
}

// startApidRuntimeSocket creates the filtered COSI state server at socketPath.
// Returns the gRPC server for cleanup (caller should defer server.Stop()).
//
// Ref: internal/app/machined/pkg/system/services/apid.go PreFunc (lines 77-118).
func startApidRuntimeSocket(ctx context.Context, st state.State, socketPath string, logger *zap.Logger) (*grpc.Server, error) {
	resources := state.Filter(st, apidResourceFilter)

	dir := filepath.Dir(socketPath)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, fmt.Errorf("creating socket directory %s: %w", dir, err)
	}

	if err := os.RemoveAll(socketPath); err != nil {
		return nil, fmt.Errorf("removing stale socket %s: %w", socketPath, err)
	}

	listener, err := (&net.ListenConfig{}).Listen(ctx, "unix", socketPath)
	if err != nil {
		return nil, fmt.Errorf("listening on %s: %w", socketPath, err)
	}

	srv := grpc.NewServer(grpc.SharedWriteBuffer(true))
	v1alpha1.RegisterStateServer(srv, server.NewState(resources))

	logger.Info("apid runtime socket listening", zap.String("path", socketPath))

	go srv.Serve(listener) //nolint:errcheck

	return srv, nil
}

// runApid waits for secrets.API, creates the filtered runtime socket, then
// starts apid as a subprocess. Blocks until apid exits or ctx is cancelled.
func runApid(ctx context.Context, st state.State, apidPath string, logger *zap.Logger) error {
	logger.Info("waiting for secrets.API before starting apid")

	cond := secrets.NewAPIReadyCondition(st)
	if err := cond.Wait(ctx); err != nil {
		return fmt.Errorf("waiting for API certs: %w", err)
	}

	logger.Info("secrets.API ready, creating runtime socket")

	runtimeServer, err := startApidRuntimeSocket(ctx, st, constants.APIRuntimeSocketPath, logger)
	if err != nil {
		return fmt.Errorf("creating runtime socket: %w", err)
	}
	defer runtimeServer.Stop()

	logger.Info("starting apid", zap.String("path", apidPath))

	cmd := exec.CommandContext(ctx, apidPath)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("starting apid: %w", err)
	}

	logger.Info("apid started", zap.Int("pid", cmd.Process.Pid))

	if err := cmd.Wait(); err != nil {
		if ctx.Err() != nil {
			return nil
		}
		return fmt.Errorf("apid exited: %w", err)
	}

	return nil
}
