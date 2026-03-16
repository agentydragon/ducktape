// Apid subprocess management for kubespand.
//
// Follows the Talos pattern: machined's service manager uses
// secrets.NewAPIReadyCondition to wait for secrets.API before starting apid.
// Here, kubespand polls the in-memory COSI state for secrets.API (same
// state the controllers write to), then starts apid as a subprocess.
//
// Ref: internal/app/machined/pkg/system/services/apid.go (Condition method)
// Ref: pkg/machinery/resources/secrets/condition.go (APIReadyCondition)
package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"

	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/resources/secrets"
	"go.uber.org/zap"
)

// runApid waits for secrets.API to appear in COSI state, then starts apid
// as a subprocess. Blocks until apid exits or ctx is cancelled.
//
// This mirrors Talos's APIReadyCondition: the service manager watches for
// secrets.API via state.Watch on the in-memory state before starting the
// apid container. We use the same condition from the Talos secrets package.
func runApid(ctx context.Context, st state.State, apidPath string, logger *zap.Logger) error {
	logger.Info("waiting for secrets.API before starting apid")

	// Use Talos's own APIReadyCondition which watches for secrets.API in COSI state.
	// This is the same condition that Talos's machined service manager uses.
	cond := secrets.NewAPIReadyCondition(st)
	if err := cond.Wait(ctx); err != nil {
		return fmt.Errorf("waiting for API certs: %w", err)
	}

	logger.Info("secrets.API ready, starting apid", zap.String("path", apidPath))

	cmd := exec.CommandContext(ctx, apidPath)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("starting apid: %w", err)
	}

	logger.Info("apid started", zap.Int("pid", cmd.Process.Pid))

	if err := cmd.Wait(); err != nil {
		// Context cancellation causes the process to be killed, which is expected.
		if ctx.Err() != nil {
			return nil
		}
		return fmt.Errorf("apid exited: %w", err)
	}

	return nil
}
