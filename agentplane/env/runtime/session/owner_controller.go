package session

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

// sessionOwnerController creates one live Session from each durable record.
type sessionOwnerController struct{ manager *Manager }

func (*sessionOwnerController) Name() string { return "session-owner" }

func (c *sessionOwnerController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycle.Namespace, Type: retained.SessionRecordType, Kind: controller.InputQPrimary},
			{Namespace: lifecycle.Namespace, Type: ownerRetryType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: OwnerStatusType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(c.manager.workers)),
	}
}

func (*sessionOwnerController) MapInput(_ context.Context, _ *zap.Logger, _ controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	return []resource.Pointer{resource.NewMetadata(lifecycle.Namespace, retained.SessionRecordType, ptr.ID(), resource.VersionUndefined)}, nil
}

func (c *sessionOwnerController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	if _, err := r.Get(ctx, ptr); state.IsNotFoundError(err) {
		return nil
	} else if err != nil {
		return err
	}
	c.manager.mu.Lock()
	failed := c.manager.ownerErrors[ptr.ID()] != nil
	c.manager.mu.Unlock()
	if failed {
		return nil
	}
	if _, err := c.manager.load(ctx, ptr.ID()); err != nil {
		if errors.Is(err, context.Canceled) && ctx.Err() != nil {
			return nil
		}
		failure := fmt.Errorf("recover Session %s: %w", ptr.ID(), err)
		statusFailed := false
		if _, publishErr := lifecycle.Write(ctx, r, ptr.ID(), OwnerStatus{Error: failure.Error()}); publishErr != nil {
			failure = errors.Join(failure, publishErr)
			statusFailed = true
		}
		c.manager.loadMu.Lock()
		_, fresh := c.manager.freshReservations[ptr.ID()]
		retryable := fresh && !statusFailed && c.manager.Get(ptr.ID()) == nil
		c.manager.loadMu.Unlock()
		c.manager.ownerFailed(ptr.ID(), failure, !retryable)
		return nil
	}
	_, err := lifecycle.Write(ctx, r, ptr.ID(), OwnerStatus{Ready: true})
	if err != nil {
		c.manager.ownerFailed(ptr.ID(), fmt.Errorf("publish Session %s owner status: %w", ptr.ID(), err), true)
		return nil
	}
	return nil
}
