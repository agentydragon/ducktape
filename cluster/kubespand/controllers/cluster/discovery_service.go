// DiscoveryController manages the discovery service client lifecycle, publishes
// the local affiliate (produced by upstream LocalAffiliateController) to the
// discovery service, and writes discovered remote affiliates as COSI resources.
//
// It also writes network.AddressStatus for discovered public IPs (consumed by
// LocalAffiliateController for endpoint construction).
//
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go
package clusterctrl

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
	"github.com/siderolabs/talos/pkg/machinery/resources/network"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/cluster/kubespand/discovery"
)

// DiscoveryController publishes the local affiliate and writes remote affiliates.
type DiscoveryController struct {
	dm       *discovery.Manager
	cancelDM context.CancelFunc

	// Track previous publish data to avoid re-publishing unchanged data.
	lastLocalVersion  string
	lastEndpointCount int
	lastPubIPLen      int
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
		safe.Input[*cluster.Config](controller.InputWeak),
		safe.Input[*cluster.Identity](controller.InputWeak),
		safe.Input[*cluster.Affiliate](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *DiscoveryController) Outputs() []controller.Output {
	return []controller.Output{
		{
			Type: cluster.AffiliateType,
			Kind: controller.OutputShared,
		},
		{
			Type: network.AddressStatusType,
			Kind: controller.OutputShared,
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

		clusterCfg, err := safe.ReaderGetByID[*cluster.Config](ctx, r, cluster.ConfigID)
		if err != nil {
			if state.IsNotFoundError(err) {
				ctrl.stopDiscovery()
				continue
			}
			return fmt.Errorf("getting cluster config: %w", err)
		}
		clusterSpec := clusterCfg.TypedSpec()

		clusterID, err := safe.ReaderGetByID[*cluster.Identity](ctx, r, cluster.LocalIdentity)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}
			return fmt.Errorf("getting cluster identity: %w", err)
		}
		localNodeID := clusterID.TypedSpec().NodeID

		ksID, err := safe.ReaderGetByID[*kubespan.Identity](ctx, r, kubespan.LocalIdentity)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}
			return fmt.Errorf("getting kubespan identity: %w", err)
		}

		// Create discovery manager if not yet running. This must happen before
		// reading the local affiliate so that peer discovery and public IP
		// detection start immediately, even if LocalAffiliateController hasn't
		// produced its output yet.
		if ctrl.dm == nil {
			dm, createErr := discovery.NewManager(clusterSpec, ksID.TypedSpec().PublicKey, logger)
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

			logger.Info("discovery client started")
		}

		// Write network.AddressStatus for discovered public IP (consumed by
		// LocalAffiliateController for endpoint construction).
		ctrl.writePublicIPStatus(ctx, r, logger)

		// Read the local affiliate produced by upstream LocalAffiliateController.
		// If it hasn't been produced yet, skip publishing but still reconcile
		// remote affiliates below so peer discovery proceeds.
		localAffiliate, err := safe.ReaderGetByID[*cluster.Affiliate](ctx, r, localNodeID)
		if err != nil && !state.IsNotFoundError(err) {
			return fmt.Errorf("getting local affiliate: %w", err)
		}

		if localAffiliate != nil {
			// Build otherEndpoints from harvested Endpoint resources for re-announcement.
			otherEndpoints := ctrl.buildOtherEndpoints(ctx, r)

			// Publish local affiliate to discovery service when data changes.
			localVersion := localAffiliate.Metadata().Version().String()
			pubIPLen := len(ctrl.dm.GetPublicIP())
			if localVersion != ctrl.lastLocalVersion ||
				len(otherEndpoints) != ctrl.lastEndpointCount ||
				pubIPLen != ctrl.lastPubIPLen {

				if pubErr := ctrl.dm.PublishAffiliate(localAffiliate.TypedSpec(), otherEndpoints); pubErr != nil {
					logger.Error("publishing local affiliate", zap.Error(pubErr))
				} else {
					ctrl.lastLocalVersion = localVersion
					ctrl.lastEndpointCount = len(otherEndpoints)
					ctrl.lastPubIPLen = pubIPLen
					logger.Debug("published local affiliate",
						zap.Int("other_endpoints", len(otherEndpoints)),
					)
				}
			}
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

		logger.Info("discovery reconciled", zap.Int("affiliates", len(affiliates)))
		r.ResetRestartBackoff()
	}
}

// writePublicIPStatus writes a network.AddressStatus resource in the cluster
// namespace with the public IP discovered from the discovery service Hello.
func (ctrl *DiscoveryController) writePublicIPStatus(ctx context.Context, r controller.Runtime, logger *zap.Logger) {
	pubIPBytes := ctrl.dm.GetPublicIP()
	if len(pubIPBytes) == 0 {
		return
	}

	pubIP, ok := netip.AddrFromSlice(pubIPBytes)
	if !ok {
		return
	}

	if err := safe.WriterModify(ctx, r,
		network.NewAddressStatus(cluster.NamespaceName, "discovered-public-ip"),
		func(res *network.AddressStatus) error {
			res.TypedSpec().Address = netip.PrefixFrom(pubIP, pubIP.BitLen())
			return nil
		},
	); err != nil {
		logger.Warn("writing public IP status", zap.Error(err))
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
