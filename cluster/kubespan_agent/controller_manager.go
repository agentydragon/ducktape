package main

import (
	"context"
	"fmt"
	"net/netip"
	"time"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/cluster/kubespan_agent/peerstate"
	"github.com/agentydragon/ducktape/cluster/kubespan_agent/resources"
	"github.com/agentydragon/ducktape/cluster/kubespan_agent/routing"
	"github.com/agentydragon/ducktape/cluster/kubespan_agent/wireguard"
)

// PeerReconcileInterval is how often we poll WireGuard for handshake times
// and potentially cycle endpoints.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
const PeerReconcileInterval = 30 * time.Second

// ManagerController manages the KubeSpan WireGuard interface, routing rules,
// and peer state. It watches Config, Identity, and PeerSpec resources, and
// produces PeerStatus resources reflecting the live WireGuard state.
//
// This controller handles:
//   - WireGuard interface creation and configuration
//   - nftables and ip rule setup for policy routing
//   - Peer endpoint cycling based on handshake state
//   - Periodic WireGuard handshake polling
//
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
type ManagerController struct {
	wg      *wireguard.Manager
	routing *routing.Manager
	ticker  *time.Ticker
}

// Name implements controller.Controller.
func (ctrl *ManagerController) Name() string {
	return "kubespan.ManagerController"
}

// Inputs implements controller.Controller.
func (ctrl *ManagerController) Inputs() []controller.Input {
	return []controller.Input{
		safe.Input[*resources.Config](controller.InputWeak),
		safe.Input[*resources.Identity](controller.InputWeak),
		safe.Input[*resources.PeerSpec](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *ManagerController) Outputs() []controller.Output {
	return []controller.Output{
		{
			Type: resources.PeerStatusType,
			Kind: controller.OutputExclusive,
		},
	}
}

// Run implements controller.Controller.
func (ctrl *ManagerController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	ctrl.ticker = time.NewTicker(PeerReconcileInterval)
	defer ctrl.ticker.Stop()
	defer ctrl.cleanup(logger)

	// Queue periodic reconciliations via the ticker.
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case <-ctrl.ticker.C:
				r.QueueReconcile()
			}
		}
	}()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		}

		if err := ctrl.reconcile(ctx, r, logger); err != nil {
			return err
		}
	}
}

func (ctrl *ManagerController) reconcile(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	cfg, err := safe.ReaderGetByID[*resources.Config](ctx, r, resources.ConfigID)
	if err != nil {
		if state.IsNotFoundError(err) {
			return nil
		}
		return fmt.Errorf("getting config: %w", err)
	}

	id, err := safe.ReaderGetByID[*resources.Identity](ctx, r, resources.IdentityID)
	if err != nil {
		if state.IsNotFoundError(err) {
			return nil
		}
		return fmt.Errorf("getting identity: %w", err)
	}

	cfgSpec := cfg.TypedSpec()
	idSpec := id.TypedSpec()

	// Initialize WireGuard and routing managers if needed.
	if ctrl.wg == nil {
		address, parseErr := idSpec.ParsedAddress()
		if parseErr != nil {
			return fmt.Errorf("parsing identity address: %w", parseErr)
		}

		wg, wgErr := wireguard.NewManager(idSpec.PrivateKey, cfgSpec.ClusterSecret, cfgSpec.ListenPort, cfgSpec.MTU)
		if wgErr != nil {
			return fmt.Errorf("wireguard manager: %w", wgErr)
		}

		if err := wg.EnsureInterface(address); err != nil {
			wg.Close()
			return fmt.Errorf("wireguard interface: %w", err)
		}
		logger.Info("WireGuard interface ready", zap.String("interface", constants.KubeSpanLinkName))

		ctrl.wg = wg

		ctrl.routing = routing.NewManager(cfgSpec.MTU, logger)
		if err := ctrl.routing.Install(nil); err != nil {
			return fmt.Errorf("routing: %w", err)
		}
		logger.Info("routing rules installed",
			zap.Int("table", constants.KubeSpanDefaultRoutingTable),
			zap.Int("rule_priority", routing.RulePriority),
		)
	}

	// List all discovered peers.
	peerList, err := safe.ReaderListAll[*resources.PeerSpec](ctx, r)
	if err != nil {
		return fmt.Errorf("listing peer specs: %w", err)
	}

	// Read existing peer statuses to preserve state machine data.
	existingStatuses, err := safe.ReaderListAll[*resources.PeerStatus](ctx, r)
	if err != nil && !state.IsNotFoundError(err) {
		return fmt.Errorf("listing peer statuses: %w", err)
	}

	statusMap := make(map[string]*peerstate.Spec)
	for ps := range existingStatuses.All() {
		spec := ps.TypedSpec().DeepCopy()
		statusMap[ps.Metadata().ID()] = &spec
	}

	// Poll WireGuard for handshake info.
	// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (UpdateFromWireguard)
	handshakes, handshakeErr := ctrl.wg.GetPeerHandshakes()
	if handshakeErr != nil {
		logger.Warn("failed to query WireGuard handshakes", zap.Error(handshakeErr))
	}

	// Build WireGuard peer configs and update statuses.
	var wgPeers []wireguard.Peer
	var routedPrefixes []netip.Prefix

	r.StartTrackingOutputs()

	for peer := range peerList.All() {
		peerSpec := peer.TypedSpec()
		pubKey := peerSpec.PublicKey

		// Get or create status for this peer.
		ps, ok := statusMap[pubKey]
		if !ok {
			ps = &peerstate.Spec{Label: peerSpec.Label}
		}

		// Update from WireGuard handshake data.
		if handshakes != nil {
			if info, found := handshakes[pubKey]; found {
				ps.LastHandshakeTime = info.LastHandshakeTime
				ps.Endpoint = info.Endpoint
				ps.TransmitBytes = info.TransmitBytes
				ps.ReceiveBytes = info.ReceiveBytes
			}
		}

		// Calculate peer state and cycle endpoint if needed.
		ps.CalculateState()

		if ps.ShouldChangeEndpoint() {
			newEP := ps.PickNewEndpoint(peerSpec.Endpoints)
			if newEP.IsValid() {
				logger.Info("cycling endpoint",
					zap.String("peer", ps.Label),
					zap.String("old", ps.LastUsedEndpoint.String()),
					zap.String("new", newEP.String()),
				)
				ps.UpdateEndpoint(newEP)
			}
		}

		// Build WireGuard peer config.
		wgPeer := wireguard.Peer{
			PublicKey:  pubKey,
			AllowedIPs: peerSpec.AllowedIPs,
		}
		if ps.LastUsedEndpoint.IsValid() {
			wgPeer.Endpoint = ps.LastUsedEndpoint
		} else if len(peerSpec.Endpoints) > 0 {
			wgPeer.Endpoint = peerSpec.Endpoints[0]
			ps.UpdateEndpoint(peerSpec.Endpoints[0])
		}
		wgPeers = append(wgPeers, wgPeer)

		// Collect routed prefixes for nftables.
		// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (routedPeersIPs)
		if cfgSpec.ForceRouting || ps.State == kubespan.PeerStateUp {
			routedPrefixes = append(routedPrefixes, peerSpec.AllowedIPs...)
		}

		// Write PeerStatus resource.
		if err := safe.WriterModify(ctx, r, resources.NewPeerStatus(resources.Namespace, resource.ID(pubKey)), func(res *resources.PeerStatus) error {
			*res.TypedSpec() = *ps
			return nil
		}); err != nil {
			return fmt.Errorf("writing peer status %s: %w", ps.Label, err)
		}
	}

	if err := safe.CleanupOutputs[*resources.PeerStatus](ctx, r); err != nil {
		return fmt.Errorf("cleaning up peer statuses: %w", err)
	}

	// Update WireGuard peers.
	if err := ctrl.wg.ConfigurePeers(wgPeers); err != nil {
		return fmt.Errorf("configuring WireGuard peers: %w", err)
	}

	// Update nftables routed prefix sets.
	if err := ctrl.routing.Update(routedPrefixes); err != nil {
		return fmt.Errorf("updating nftables: %w", err)
	}

	r.ResetRestartBackoff()
	return nil
}

// cleanup tears down the WireGuard interface and routing rules.
func (ctrl *ManagerController) cleanup(logger *zap.Logger) {
	if ctrl.routing != nil {
		if err := ctrl.routing.Cleanup(); err != nil {
			logger.Error("routing cleanup failed", zap.Error(err))
		}
	}
	if ctrl.wg != nil {
		if err := ctrl.wg.Cleanup(); err != nil {
			logger.Error("wireguard cleanup failed", zap.Error(err))
		}
		ctrl.wg.Close()
	}
}
