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
	"bytes"
	"context"
	"fmt"
	"net/netip"
	"slices"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	clientpb "github.com/siderolabs/discovery-api/api/v1alpha1/client/pb"
	discoveryclient "github.com/siderolabs/discovery-client/pkg/client"
	"github.com/siderolabs/gen/optional"
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

	discoveryConfigVersion resource.Version
	localAffiliateID       resource.ID

	// Track previous publish data to avoid re-publishing unchanged data.
	// Upstream uses proto.Equal on the full protobuf messages; we compare the
	// COSI resource version for the affiliate (strictly better — authoritative
	// from COSI) and actual byte/struct equality for public IP and endpoints.
	lastLocalVersion   string
	lastOtherEndpoints []discoveryclient.Endpoint
	lastPublicIP       []byte
}

// Name implements controller.Controller.
func (ctrl *DiscoveryController) Name() string {
	return "kubespan.DiscoveryController"
}

// Inputs implements controller.Controller.
// cluster.Affiliate is NOT listed here — it is added dynamically via
// UpdateInputs once the local affiliate ID is known.
func (ctrl *DiscoveryController) Inputs() []controller.Input {
	return []controller.Input{
		safe.Input[*kubespan.Config](controller.InputWeak),
		safe.Input[*kubespan.Identity](controller.InputWeak),
		safe.Input[*kubespan.Endpoint](controller.InputWeak),
		safe.Input[*cluster.Config](controller.InputWeak),
		safe.Input[*cluster.Identity](controller.InputWeak),
		// Talos watches runtime.MachineResetSignal to delete the local
		// affiliate on machine reset. kubespand has no machine reset concept;
		// cleanup happens only via stopDiscovery on shutdown.
		//
		// Talos checks RegistryServiceEnabled and cleans up when discovery is
		// disabled. kubespand assumes discovery is always enabled if
		// cluster.Config exists.
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

		// Force reconnect when discovery config changes.
		if !clusterCfg.Metadata().Version().Equal(ctrl.discoveryConfigVersion) {
			ctrl.stopDiscovery()
		}

		clusterID, err := safe.ReaderGetByID[*cluster.Identity](ctx, r, cluster.LocalIdentity)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}

			return fmt.Errorf("getting cluster identity: %w", err)
		}
		localNodeID := clusterID.TypedSpec().NodeID

		// Dynamically register the specific local affiliate as an input so
		// COSI only triggers reconciles for that ID, not all affiliates.
		// This avoids a feedback loop where writing remote affiliates
		// triggers self-reconciliation.
		if ctrl.localAffiliateID != resource.ID(localNodeID) {
			ctrl.localAffiliateID = resource.ID(localNodeID)

			if err = r.UpdateInputs(append(ctrl.Inputs(),
				controller.Input{
					Namespace: cluster.NamespaceName,
					Type:      cluster.AffiliateType,
					ID:        optional.Some(ctrl.localAffiliateID),
					Kind:      controller.InputWeak,
				},
			)); err != nil {
				return err
			}

			ctrl.stopDiscovery()
		}

		ksID, err := safe.ReaderGetByID[*kubespan.Identity](ctx, r, kubespan.LocalIdentity)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}

			return fmt.Errorf("getting kubespan identity: %w", err)
		}

		// Read the local affiliate produced by upstream LocalAffiliateController.
		localAffiliate, err := safe.ReaderGetByID[*cluster.Affiliate](ctx, r, localNodeID)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}

			return fmt.Errorf("getting local affiliate: %w", err)
		}

		// Create discovery manager if not yet running.
		if ctrl.dm == nil {
			dm, createErr := discovery.NewManager(clusterSpec, ksID.TypedSpec().PublicKey, logger)
			if createErr != nil {
				return fmt.Errorf("creating discovery manager: %w", createErr)
			}

			var dmCtx context.Context
			dmCtx, ctrl.cancelDM = context.WithCancel(ctx)
			ctrl.dm = dm
			ctrl.discoveryConfigVersion = clusterCfg.Metadata().Version()

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

		// Build otherEndpoints from harvested Endpoint resources for re-announcement.
		otherEndpoints := ctrl.buildOtherEndpoints(ctx, r)

		// Publish local affiliate to discovery service when data changes.
		localVersion := localAffiliate.Metadata().Version().String()
		publicIP := ctrl.dm.GetPublicIP()

		if localVersion != ctrl.lastLocalVersion ||
			!equalOtherEndpoints(otherEndpoints, ctrl.lastOtherEndpoints) ||
			!bytes.Equal(publicIP, ctrl.lastPublicIP) {

			if pubErr := ctrl.dm.PublishAffiliate(localAffiliate.TypedSpec(), otherEndpoints); pubErr != nil {
				logger.Error("publishing local affiliate", zap.Error(pubErr))
			} else {
				ctrl.lastLocalVersion = localVersion
				ctrl.lastOtherEndpoints = otherEndpoints
				ctrl.lastPublicIP = publicIP
				logger.Debug("published local affiliate",
					zap.Int("other_endpoints", len(otherEndpoints)),
					zap.Int("kubespan_endpoints", len(localAffiliate.TypedSpec().KubeSpan.Endpoints)),
					zap.Int("addresses", len(localAffiliate.TypedSpec().Addresses)),
				)
			}
		}

		// Reconcile cluster.Affiliate resources from discovered peers.
		// Talos writes to cluster.RawNamespaceName with "service/" ID prefix,
		// then AffiliateMergeController merges into cluster.NamespaceName.
		// kubespand writes directly to cluster.NamespaceName (single discovery
		// source, no merge needed).
		affiliates := ctrl.dm.GetAffiliates()

		r.StartTrackingOutputs()

		for pubKey, affSpec := range affiliates {
			logger.Debug("affiliate from discovery",
				zap.String("pubkey", pubKey),
				zap.String("hostname", affSpec.Hostname),
				zap.Int("endpoints", len(affSpec.KubeSpan.Endpoints)),
				zap.Int("addresses", len(affSpec.Addresses)),
			)
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
// Results are sorted by AffiliateID for stable comparison across reconcile cycles.
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

	// Sort for stable comparison across reconcile cycles (map iteration
	// order is non-deterministic).
	slices.SortFunc(result, func(a, b discoveryclient.Endpoint) int {
		if a.AffiliateID < b.AffiliateID {
			return -1
		}
		if a.AffiliateID > b.AffiliateID {
			return 1
		}

		return 0
	})

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
	ctrl.lastLocalVersion = ""
	ctrl.lastOtherEndpoints = nil
	ctrl.lastPublicIP = nil
}

// equalOtherEndpoints compares two slices of discovery endpoints.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (equalOtherEndpoints)
func equalOtherEndpoints(a, b []discoveryclient.Endpoint) bool {
	if len(a) != len(b) {
		return false
	}

	for i := range a {
		if a[i].AffiliateID != b[i].AffiliateID {
			return false
		}

		if !equalPBEndpoints(a[i].Endpoints, b[i].Endpoints) {
			return false
		}
	}

	return true
}

// equalPBEndpoints compares two slices of protobuf Endpoint messages by value.
// Ref: talos/internal/app/machined/pkg/controllers/cluster/discovery_service.go (equalEndpoints)
func equalPBEndpoints(a, b []*clientpb.Endpoint) bool {
	if len(a) != len(b) {
		return false
	}

	for i := range a {
		if !bytes.Equal(a[i].Ip, b[i].Ip) || a[i].Port != b[i].Port {
			return false
		}
	}

	return true
}
