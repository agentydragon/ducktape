package main

import (
	"context"
	"fmt"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/cluster/kubespan_agent/identity"
)

// IdentityController watches Config and produces the node's KubeSpan Identity.
//
// It loads or creates a WireGuard keypair and derives the KubeSpan ULA address
// from the cluster ID and the machine's MAC address.
//
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/identity.go
type IdentityController struct {
	cachedID *kubespan.IdentitySpec
}

// Name implements controller.Controller.
func (ctrl *IdentityController) Name() string {
	return "kubespan.IdentityController"
}

// Inputs implements controller.Controller.
func (ctrl *IdentityController) Inputs() []controller.Input {
	return []controller.Input{
		safe.Input[*kubespan.Config](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *IdentityController) Outputs() []controller.Output {
	return []controller.Output{
		{
			Type: kubespan.IdentityType,
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

		cfg, err := safe.ReaderGetByID[*kubespan.Config](ctx, r, kubespan.ConfigID)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}
			return fmt.Errorf("getting config: %w", err)
		}

		cfgSpec := cfg.TypedSpec()

		if ctrl.cachedID == nil {
			mac, err := identity.DetectMAC()
			if err != nil {
				return fmt.Errorf("detecting MAC: %w", err)
			}

			id, err := identity.LoadOrCreate(agentCfg.IdentityFile, cfgSpec.ClusterID)
			if err != nil {
				return fmt.Errorf("loading identity: %w", err)
			}

			if err := identity.UpdateAddress(id, cfgSpec.ClusterID, mac); err != nil {
				return fmt.Errorf("computing address: %w", err)
			}

			ctrl.cachedID = id

			logger.Info("identity ready",
				zap.String("public_key", id.PublicKey),
				zap.Stringer("subnet", id.Subnet),
				zap.Stringer("address", id.Address),
			)
		}

		if err := safe.WriterModify(ctx, r,
			kubespan.NewIdentity(kubespan.NamespaceName, kubespan.LocalIdentity),
			func(res *kubespan.Identity) error {
				*res.TypedSpec() = *ctrl.cachedID
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing identity: %w", err)
		}

		r.ResetRestartBackoff()
	}
}
