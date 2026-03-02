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
	"github.com/siderolabs/go-pointer"
	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"go.uber.org/zap"
	"golang.zx2c4.com/wireguard/wgctrl/wgtypes"

	"github.com/agentydragon/ducktape/cluster/kubespan_agent/peerstate"
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
		safe.Input[*kubespan.Config](controller.InputWeak),
		safe.Input[*kubespan.Identity](controller.InputWeak),
		safe.Input[*kubespan.PeerSpec](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *ManagerController) Outputs() []controller.Output {
	return []controller.Output{
		{
			Type: kubespan.PeerStatusType,
			Kind: controller.OutputExclusive,
		},
	}
}

// Run implements controller.Controller.
func (ctrl *ManagerController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	ctrl.ticker = time.NewTicker(PeerReconcileInterval)
	defer ctrl.ticker.Stop()
	defer ctrl.cleanup(logger)

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
	cfg, err := safe.ReaderGetByID[*kubespan.Config](ctx, r, kubespan.ConfigID)
	if err != nil {
		if state.IsNotFoundError(err) {
			return nil
		}
		return fmt.Errorf("getting config: %w", err)
	}

	id, err := safe.ReaderGetByID[*kubespan.Identity](ctx, r, kubespan.LocalIdentity)
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
		wg, wgErr := wireguard.NewManager(idSpec.PrivateKey, cfgSpec.SharedSecret, agentCfg.ListenPort, int(cfgSpec.MTU))
		if wgErr != nil {
			return fmt.Errorf("wireguard manager: %w", wgErr)
		}

		if err := wg.EnsureInterface(idSpec.Address); err != nil {
			wg.Close()
			return fmt.Errorf("wireguard interface: %w", err)
		}
		logger.Info("WireGuard interface ready", zap.String("interface", constants.KubeSpanLinkName))

		ctrl.wg = wg

		ctrl.routing = routing.NewManager(int(cfgSpec.MTU), logger)
		if err := ctrl.routing.Install(nil); err != nil {
			return fmt.Errorf("routing: %w", err)
		}
		logger.Info("routing rules installed",
			zap.Int("table", constants.KubeSpanDefaultRoutingTable),
		)
	}

	// List all discovered peers.
	peerList, err := safe.ReaderListAll[*kubespan.PeerSpec](ctx, r)
	if err != nil {
		return fmt.Errorf("listing peer specs: %w", err)
	}

	// Read existing peer statuses to preserve state machine data.
	existingStatuses, err := safe.ReaderListAll[*kubespan.PeerStatus](ctx, r)
	if err != nil && !state.IsNotFoundError(err) {
		return fmt.Errorf("listing peer statuses: %w", err)
	}

	statusMap := make(map[string]*kubespan.PeerStatusSpec)
	for ps := range existingStatuses.All() {
		spec := ps.TypedSpec().DeepCopy()
		statusMap[ps.Metadata().ID()] = &spec
	}

	// Poll WireGuard for peer state.
	// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (UpdateFromWireguard)
	wgPeerList, handshakeErr := ctrl.wg.GetPeers()
	if handshakeErr != nil {
		logger.Warn("failed to query WireGuard peers", zap.Error(handshakeErr))
	}

	// Index WireGuard peers by public key for lookup.
	wgPeerMap := make(map[string]int, len(wgPeerList))
	for i, p := range wgPeerList {
		wgPeerMap[p.PublicKey.String()] = i
	}

	// Build WireGuard peer configs and update statuses.
	var wgPeers []wgtypes.PeerConfig
	var routedPrefixes []netip.Prefix

	r.StartTrackingOutputs()

	for peer := range peerList.All() {
		peerSpec := peer.TypedSpec()
		pubKey := peer.Metadata().ID()

		// Get or create status for this peer.
		ps, ok := statusMap[pubKey]
		if !ok {
			ps = &kubespan.PeerStatusSpec{Label: peerSpec.Label}
		}

		// Update from WireGuard peer data.
		if idx, found := wgPeerMap[pubKey]; found {
			peerstate.UpdateFromWireguard(ps, wgPeerList[idx])
		}

		// Calculate peer state and cycle endpoint if needed.
		peerstate.CalculateState(ps)

		if peerstate.ShouldChangeEndpoint(ps) {
			newEP := peerstate.PickNewEndpoint(ps, peerSpec.Endpoints)
			if newEP.IsValid() {
				logger.Info("cycling endpoint",
					zap.String("peer", ps.Label),
					zap.Stringer("old", ps.LastUsedEndpoint),
					zap.Stringer("new", newEP),
				)
				peerstate.UpdateEndpoint(ps, newEP)
			}
		}

		// Build WireGuard peer config directly.
		wgKey, keyErr := wgtypes.ParseKey(pubKey)
		if keyErr != nil {
			logger.Warn("skipping peer with invalid key", zap.String("key", pubKey), zap.Error(keyErr))
			continue
		}
		peerCfg := wgtypes.PeerConfig{
			PublicKey:                   wgKey,
			PresharedKey:                ctrl.wg.PresharedKey(),
			PersistentKeepaliveInterval: pointer.To(constants.KubeSpanDefaultPeerKeepalive),
			ReplaceAllowedIPs:           true,
			AllowedIPs:                  wireguard.PrefixesToIPNets(peerSpec.AllowedIPs),
		}
		if ps.LastUsedEndpoint.IsValid() {
			peerCfg.Endpoint = wireguard.AddrPortToUDPAddr(ps.LastUsedEndpoint)
		} else if len(peerSpec.Endpoints) > 0 {
			peerCfg.Endpoint = wireguard.AddrPortToUDPAddr(peerSpec.Endpoints[0])
			peerstate.UpdateEndpoint(ps, peerSpec.Endpoints[0])
		}
		wgPeers = append(wgPeers, peerCfg)

		// Collect routed prefixes for nftables.
		// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (routedPeersIPs)
		if cfgSpec.ForceRouting || ps.State == kubespan.PeerStateUp {
			routedPrefixes = append(routedPrefixes, peerSpec.AllowedIPs...)
		}

		// Write PeerStatus resource.
		if err := safe.WriterModify(ctx, r,
			kubespan.NewPeerStatus(kubespan.NamespaceName, resource.ID(pubKey)),
			func(res *kubespan.PeerStatus) error {
				*res.TypedSpec() = *ps
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing peer status %s: %w", ps.Label, err)
		}
	}

	if err := safe.CleanupOutputs[*kubespan.PeerStatus](ctx, r); err != nil {
		return fmt.Errorf("cleaning up peer statuses: %w", err)
	}

	if err := ctrl.wg.ConfigurePeers(wgPeers); err != nil {
		return fmt.Errorf("configuring WireGuard peers: %w", err)
	}

	if err := ctrl.routing.Update(routedPrefixes); err != nil {
		return fmt.Errorf("updating nftables: %w", err)
	}

	r.ResetRestartBackoff()
	return nil
}

// cleanup tears down the WireGuard interface and routing rules.
// TODO: align cleanup with COSI resource teardown (Talos writes LinkSpec/AddressSpec/RouteSpec)
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
