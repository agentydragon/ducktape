package main

import (
	"context"
	"fmt"
	"net/netip"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	clientpb "github.com/siderolabs/discovery-api/api/v1alpha1/client/pb"
	discoveryclient "github.com/siderolabs/discovery-client/pkg/client"
	"github.com/siderolabs/talos/pkg/machinery/resources/cluster"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/cluster/kubespan_agent/discovery"
	"github.com/agentydragon/ducktape/cluster/kubespan_agent/k8snet"
)

// DiscoveryController watches Config + Identity and produces cluster.Affiliate
// resources by communicating with the Talos discovery service.
//
// It manages the lifecycle of the discovery Manager: creating it when Config and
// Identity become available, forwarding discovery notifications to the COSI
// event loop, and cleaning up on shutdown.
//
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go
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
		safe.Input[*kubespan.Endpoint](controller.InputWeak),
		safe.Input[*k8snet.KubernetesNetworks](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *DiscoveryController) Outputs() []controller.Output {
	return []controller.Output{
		{
			Type: cluster.AffiliateType,
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

		_, err := safe.ReaderGetByID[*kubespan.Config](ctx, r, kubespan.ConfigID)
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

		idSpec := id.TypedSpec()

		// Build otherEndpoints from harvested Endpoint resources for re-announcement.
		otherEndpoints := ctrl.buildOtherEndpoints(ctx, r)

		// Read KubernetesNetworks resource for local AdditionalAddresses (PodCIDRs + ServiceCIDRs).
		var additionalAddresses []netip.Prefix
		if nets, netErr := safe.ReaderGetByID[*k8snet.KubernetesNetworks](ctx, r, k8snet.ID); netErr == nil {
			additionalAddresses = nets.TypedSpec().Prefixes
		}

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

			if pubErr := dm.PublishLocal(agentCfg, idSpec, agentCfg.ListenPort, otherEndpoints, additionalAddresses); pubErr != nil {
				logger.Error("publishing local affiliate", zap.Error(pubErr))
			}

			logger.Info("discovery client started")
		}

		// Re-publish to keep TTL fresh and update harvested endpoints + additional addresses.
		if pubErr := ctrl.dm.PublishLocal(agentCfg, id.TypedSpec(), agentCfg.ListenPort, otherEndpoints, additionalAddresses); pubErr != nil {
			logger.Warn("re-publishing local affiliate", zap.Error(pubErr))
		}

		// Reconcile cluster.Affiliate resources from discovered peers.
		affiliates := ctrl.dm.GetAffiliates()

		r.StartTrackingOutputs()

		for pubKey, affSpec := range affiliates {
			if err := safe.WriterModify(ctx, r,
				cluster.NewAffiliate(cluster.NamespaceName, resource.ID(pubKey)),
				func(res *cluster.Affiliate) error {
					*res.TypedSpec() = affSpec
					return nil
				},
			); err != nil {
				return fmt.Errorf("writing affiliate %s: %w", affSpec.Hostname, err)
			}
		}

		if err := safe.CleanupOutputs[*cluster.Affiliate](ctx, r); err != nil {
			return fmt.Errorf("cleaning up affiliates: %w", err)
		}

		logger.Debug("discovery reconciled", zap.Int("affiliates", len(affiliates)))
		r.ResetRestartBackoff()
	}
}

// buildOtherEndpoints reads harvested kubespan.Endpoint resources and converts
// them to discoveryclient.Endpoint for re-announcement via the discovery service.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (pbOtherEndpoints)
func (ctrl *DiscoveryController) buildOtherEndpoints(ctx context.Context, r controller.Runtime) []discoveryclient.Endpoint {
	endpoints, err := safe.ReaderListAll[*kubespan.Endpoint](ctx, r)
	if err != nil {
		return nil
	}

	// Group endpoints by AffiliateID.
	byAffiliate := make(map[string][]netip.AddrPort)
	for ep := range endpoints.All() {
		spec := ep.TypedSpec()
		if spec.AffiliateID == "" || !spec.Endpoint.IsValid() {
			continue
		}
		byAffiliate[spec.AffiliateID] = append(byAffiliate[spec.AffiliateID], spec.Endpoint)
	}

	var result []discoveryclient.Endpoint
	for affID, addrPorts := range byAffiliate {
		var pbEndpoints []*clientpb.Endpoint
		for _, ap := range addrPorts {
			ipBytes, _ := ap.Addr().MarshalBinary()
			pbEndpoints = append(pbEndpoints, &clientpb.Endpoint{
				Ip:   ipBytes,
				Port: uint32(ap.Port()),
			})
		}
		result = append(result, discoveryclient.Endpoint{
			AffiliateID: affID,
			Endpoints:   pbEndpoints,
		})
	}

	return result
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
