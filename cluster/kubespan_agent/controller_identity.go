package main

import (
	"context"
	"fmt"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"go.uber.org/zap"
)

// IdentityController watches Config and produces the node's KubeSpan Identity.
//
// It loads or creates a WireGuard keypair and derives the KubeSpan ULA address
// from the cluster ID and the machine's MAC address.
//
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/identity.go
type IdentityController struct{}

// Name implements controller.Controller.
func (ctrl *IdentityController) Name() string {
	return "kubespan.IdentityController"
}

// Inputs implements controller.Controller.
func (ctrl *IdentityController) Inputs() []controller.Input {
	return []controller.Input{
		safe.Input[*Config](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *IdentityController) Outputs() []controller.Output {
	return []controller.Output{
		{
			Type: IdentityType,
			Kind: controller.OutputExclusive,
		},
	}
}

// Run implements controller.Controller.
func (ctrl *IdentityController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		}

		cfg, err := safe.ReaderGetByID[*Config](ctx, r, ConfigID)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}
			return fmt.Errorf("getting config: %w", err)
		}

		cfgSpec := cfg.TypedSpec()

		mac, err := DetectMAC()
		if err != nil {
			return fmt.Errorf("detecting MAC: %w", err)
		}

		id, err := LoadOrCreateIdentity(cfgSpec.IdentityFile, cfgSpec.ClusterID)
		if err != nil {
			return fmt.Errorf("loading identity: %w", err)
		}

		if err := id.UpdateAddress(cfgSpec.ClusterID, mac); err != nil {
			return fmt.Errorf("computing address: %w", err)
		}

		logger.Info("identity ready",
			zap.String("public_key", id.PublicKey),
			zap.String("subnet", id.Subnet),
			zap.String("address", id.Address),
		)

		if err := safe.WriterModify(ctx, r, NewIdentity(KubespanNamespace, IdentityID), func(res *Identity) error {
			*res.TypedSpec() = *id
			return nil
		}); err != nil {
			return fmt.Errorf("writing identity: %w", err)
		}

		r.ResetRestartBackoff()
	}
}
