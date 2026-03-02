package main

import (
	"context"
	"fmt"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/cluster/kubespan_agent/discovery"
)

// DiscoveryController watches Config + Identity and produces PeerSpec resources
// by communicating with the Talos discovery service.
//
// It manages the lifecycle of the discovery Manager: creating it when Config and
// Identity become available, forwarding discovery notifications to the COSI
// event loop, and cleaning up on shutdown.
//
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/peer_spec.go
type DiscoveryController struct {
	dm       *discovery.Manager
	cancelDM context.CancelFunc
}

// Name implements controller.Controller.
func (ctrl *DiscoveryController) Name() string {
	return "kubespan.DiscoveryController"
}

// Inputs implements controller.Controller.
func (ctrl *DiscoveryController) Inputs() []controller.Input {
	return []controller.Input{
		safe.Input[*kubespan.Config](controller.InputWeak),
		safe.Input[*kubespan.Identity](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *DiscoveryController) Outputs() []controller.Output {
	return []controller.Output{
		{
			Type: kubespan.PeerSpecType,
			Kind: controller.OutputExclusive,
		},
	}
}

// Run implements controller.Controller.
func (ctrl *DiscoveryController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	defer ctrl.stopDiscovery()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		}

		cfg, err := safe.ReaderGetByID[*kubespan.Config](ctx, r, kubespan.ConfigID)
		if err != nil {
			if state.IsNotFoundError(err) {
				ctrl.stopDiscovery()
				continue
			}
			return fmt.Errorf("getting config: %w", err)
		}

		id, err := safe.ReaderGetByID[*kubespan.Identity](ctx, r, kubespan.LocalIdentity)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}
			return fmt.Errorf("getting identity: %w", err)
		}

		cfgSpec := cfg.TypedSpec()
		idSpec := id.TypedSpec()
		_ = cfgSpec // used via agentCfg below

		// Create discovery manager if not yet running.
		if ctrl.dm == nil {
			dm, createErr := discovery.NewManager(agentCfg, idSpec.PublicKey, logger)
			if createErr != nil {
				return fmt.Errorf("creating discovery manager: %w", createErr)
			}

			var dmCtx context.Context
			dmCtx, ctrl.cancelDM = context.WithCancel(ctx)
			ctrl.dm = dm

			go func() {
				if runErr := dm.Run(dmCtx); runErr != nil {
					logger.Error("discovery client error", zap.Error(runErr))
				}
			}()

			// Forward discovery notifications to the COSI reconcile loop.
			go func() {
				for {
					select {
					case <-dmCtx.Done():
						return
					case <-dm.NotifyCh():
						r.QueueReconcile()
					}
				}
			}()

			if pubErr := dm.PublishLocal(agentCfg, idSpec, agentCfg.ListenPort); pubErr != nil {
				logger.Error("publishing local affiliate", zap.Error(pubErr))
			}

			logger.Info("discovery client started")
		}

		// Re-publish to keep TTL fresh.
		if pubErr := ctrl.dm.PublishLocal(agentCfg, id.TypedSpec(), agentCfg.ListenPort); pubErr != nil {
			logger.Warn("re-publishing local affiliate", zap.Error(pubErr))
		}

		// Reconcile PeerSpec resources from discovered peers.
		peers := ctrl.dm.GetPeers()

		r.StartTrackingOutputs()

		for pubKey, peerSpec := range peers {
			if err := safe.WriterModify(ctx, r,
				kubespan.NewPeerSpec(kubespan.NamespaceName, resource.ID(pubKey)),
				func(res *kubespan.PeerSpec) error {
					*res.TypedSpec() = peerSpec
					return nil
				},
			); err != nil {
				return fmt.Errorf("writing peer spec %s: %w", peerSpec.Label, err)
			}
		}

		if err := safe.CleanupOutputs[*kubespan.PeerSpec](ctx, r); err != nil {
			return fmt.Errorf("cleaning up peer specs: %w", err)
		}

		logger.Debug("discovery reconciled", zap.Int("peers", len(peers)))
		r.ResetRestartBackoff()
	}
}

// stopDiscovery shuts down the discovery manager if running.
func (ctrl *DiscoveryController) stopDiscovery() {
	if ctrl.dm != nil {
		ctrl.dm.DeleteLocalAffiliate()
	}
	if ctrl.cancelDM != nil {
		ctrl.cancelDM()
		ctrl.cancelDM = nil
	}
	ctrl.dm = nil
}
