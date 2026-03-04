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
	"github.com/siderolabs/talos/pkg/machinery/nethelpers"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"github.com/siderolabs/talos/pkg/machinery/resources/network"
	"go.uber.org/zap"
	"golang.zx2c4.com/wireguard/wgctrl/wgtypes"

	kubespanadapter "github.com/agentydragon/ducktape/cluster/kubespan_agent/peerstate"
	routing "github.com/agentydragon/ducktape/cluster/kubespan_agent/routing"
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
// TODO: consider pulling from upstream Talos manager.go. Key differences:
//   - Talos writes COSI network.LinkSpec/AddressSpec/RouteSpec; kubespand calls wgctrl/netlink directly
//   - Talos uses WireguardClientFactory/RulesManagerFactory for testability
//   - Same peer state machine and nftables chain writing pattern
//
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
type ManagerController struct {
	wg     *wireguard.Manager
	rules  routing.RulesManager
	ticker *time.Ticker
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
		{
			Type: network.NfTablesChainType,
			Kind: controller.OutputExclusive,
		},
	}
}

// Run implements controller.Controller.
func (ctrl *ManagerController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	ctrl.ticker = time.NewTicker(PeerReconcileInterval)
	defer ctrl.ticker.Stop()

	// Only clean up on permanent shutdown (context cancellation), not on
	// transient reconcile errors. COSI restarts the controller after errors,
	// and the existing WireGuard interface + nftables state should be reused.
	// Cleaning up on every restart triggers nf_tables_commit_release() which
	// holds the kernel's commit_mutex, causing EBUSY on the next install.
	defer func() {
		if ctx.Err() != nil {
			ctrl.cleanup(logger)
		}
	}()

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

	// Initialize WireGuard and routing rules if needed.
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

		// IP policy rules (fwmark → routing table). Nftables are now
		// managed by NfTablesChainController via COSI resources.
		ctrl.rules = routing.NewRulesManager(
			uint8(constants.KubeSpanDefaultRoutingTable),
			constants.KubeSpanDefaultForceFirewallMark,
			constants.KubeSpanDefaultFirewallMask,
		)
		if err := ctrl.rules.Cleanup(); err != nil {
			logger.Warn("ip rules cleanup failed (may be first run)", zap.Error(err))
		}
		if err := ctrl.rules.Install(); err != nil {
			return fmt.Errorf("ip rules: %w", err)
		}
		if err := routing.InstallRoutes(int(cfgSpec.MTU)); err != nil {
			return fmt.Errorf("routes: %w", err)
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
			logger.Info("configuring peer",
				zap.String("label", peerSpec.Label),
				zap.String("public_key", pubKey),
				zap.Stringer("address", peerSpec.Address),
				zap.Int("allowed_ips", len(peerSpec.AllowedIPs)),
			)
		}

		// Update from WireGuard peer data.
		if idx, found := wgPeerMap[pubKey]; found {
			kubespanadapter.PeerStatusSpec(ps).UpdateFromWireguard(wgPeerList[idx])
		}

		// Calculate peer state and cycle endpoint if needed.
		kubespanadapter.PeerStatusSpec(ps).CalculateState()

		if kubespanadapter.PeerStatusSpec(ps).ShouldChangeEndpoint() {
			newEP := kubespanadapter.PeerStatusSpec(ps).PickNewEndpoint(peerSpec.Endpoints)
			if newEP.IsValid() {
				logger.Info("cycling endpoint",
					zap.String("peer", ps.Label),
					zap.Stringer("old", ps.LastUsedEndpoint),
					zap.Stringer("new", newEP),
				)
				kubespanadapter.PeerStatusSpec(ps).UpdateEndpoint(newEP)
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
			kubespanadapter.PeerStatusSpec(ps).UpdateEndpoint(peerSpec.Endpoints[0])
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

	if err := ctrl.writeNfTablesChains(ctx, r, routedPrefixes, cfgSpec.MTU); err != nil {
		return fmt.Errorf("writing nftables chains: %w", err)
	}

	r.ResetRestartBackoff()
	return nil
}

// writeNfTablesChains writes NfTablesChain COSI resources for the prerouting
// and output chains. The NfTablesChainController watches these and applies
// them to the kernel nftables subsystem.
//
// This is the COSI equivalent of the old routing.Manager.Update() which built
// nftables expressions directly. The rule semantics are identical.
func (ctrl *ManagerController) writeNfTablesChains(
	ctx context.Context,
	r controller.Runtime,
	routedPrefixes []netip.Prefix,
	mtu uint32,
) error {
	// Prerouting chain: mark incoming packets for routed prefixes.
	if err := safe.WriterModify(ctx, r,
		network.NewNfTablesChain(network.NamespaceName, "kubespan_prerouting"),
		func(chain *network.NfTablesChain) error {
			spec := chain.TypedSpec()
			spec.Type = nethelpers.ChainTypeFilter
			spec.Hook = nethelpers.ChainHookPrerouting
			spec.Priority = nethelpers.ChainPriorityRaw
			spec.Policy = nethelpers.VerdictAccept
			spec.Rules = buildPreroutingRules(routedPrefixes)
			return nil
		},
	); err != nil {
		return fmt.Errorf("prerouting chain: %w", err)
	}

	// Output chain: mark outgoing packets, clamp MSS.
	if err := safe.WriterModify(ctx, r,
		network.NewNfTablesChain(network.NamespaceName, "kubespan_outgoing"),
		func(chain *network.NfTablesChain) error {
			spec := chain.TypedSpec()
			spec.Type = nethelpers.ChainTypeRoute
			spec.Hook = nethelpers.ChainHookOutput
			spec.Priority = nethelpers.ChainPriorityRaw
			spec.Policy = nethelpers.VerdictAccept
			spec.Rules = buildOutputRules(routedPrefixes, uint16(mtu))
			return nil
		},
	); err != nil {
		return fmt.Errorf("output chain: %w", err)
	}

	return nil
}

// buildPreroutingRules produces NfTablesRule specs for the prerouting chain.
// Transliteration of routing.go:tryInstallNftables prerouting section.
func buildPreroutingRules(routedPrefixes []netip.Prefix) []network.NfTablesRule {
	acceptVerdict := nethelpers.VerdictAccept

	rules := []network.NfTablesRule{
		// Skip packets already marked by WireGuard (mark & 0x60 == 0x20 → accept).
		{
			MatchMark: &network.NfTablesMark{
				Mask:  constants.KubeSpanDefaultFirewallMask,
				Xor:   0,
				Value: constants.KubeSpanDefaultFirewallMark,
			},
			Verdict: &acceptVerdict,
		},
	}

	if len(routedPrefixes) > 0 {
		// Mark destination-matched packets with force mark (0x40).
		rules = append(rules, network.NfTablesRule{
			MatchDestinationAddress: &network.NfTablesAddressMatch{
				IncludeSubnets: routedPrefixes,
			},
			SetMark: &network.NfTablesMark{
				Mask: ^uint32(constants.KubeSpanDefaultForceFirewallMark),
				Xor:  constants.KubeSpanDefaultForceFirewallMark,
			},
			Verdict: &acceptVerdict,
		})
	}

	return rules
}

// buildOutputRules produces NfTablesRule specs for the output chain.
// Transliteration of routing.go:tryInstallNftables output section.
func buildOutputRules(routedPrefixes []netip.Prefix, mtu uint16) []network.NfTablesRule {
	acceptVerdict := nethelpers.VerdictAccept

	rules := []network.NfTablesRule{
		// Skip packets already marked by WireGuard.
		{
			MatchMark: &network.NfTablesMark{
				Mask:  constants.KubeSpanDefaultFirewallMask,
				Xor:   0,
				Value: constants.KubeSpanDefaultFirewallMark,
			},
			Verdict: &acceptVerdict,
		},
		// Skip loopback traffic.
		{
			MatchOIfName: &network.NfTablesIfNameMatch{
				InterfaceNames: []string{"lo"},
				Operator:       nethelpers.OperatorEqual,
			},
			Verdict: &acceptVerdict,
		},
	}

	if len(routedPrefixes) > 0 {
		// MSS clamp for routed destinations.
		if mtu > 0 {
			rules = append(rules, network.NfTablesRule{
				MatchDestinationAddress: &network.NfTablesAddressMatch{
					IncludeSubnets: routedPrefixes,
				},
				ClampMSS: &network.NfTablesClampMSS{
					MTU: mtu,
				},
			})
		}

		// Mark destination-matched packets with force mark.
		rules = append(rules, network.NfTablesRule{
			MatchDestinationAddress: &network.NfTablesAddressMatch{
				IncludeSubnets: routedPrefixes,
			},
			SetMark: &network.NfTablesMark{
				Mask: ^uint32(constants.KubeSpanDefaultForceFirewallMark),
				Xor:  constants.KubeSpanDefaultForceFirewallMark,
			},
			Verdict: &acceptVerdict,
		})
	}

	return rules
}

// cleanup tears down the WireGuard interface and routing rules.
// TODO: align cleanup with COSI resource teardown (Talos writes LinkSpec/AddressSpec/RouteSpec)
func (ctrl *ManagerController) cleanup(logger *zap.Logger) {
	// IP policy rules cleanup. Nftables cleanup is handled by the
	// NfTablesChainController when its COSI resources are torn down.
	if ctrl.rules != nil {
		if err := ctrl.rules.Cleanup(); err != nil {
			logger.Error("ip rules cleanup failed", zap.Error(err))
		}
		ctrl.rules = nil
	}
	if ctrl.wg != nil {
		if err := ctrl.wg.Cleanup(); err != nil {
			logger.Error("wireguard cleanup failed", zap.Error(err))
		}
		ctrl.wg.Close()
		ctrl.wg = nil
	}
}
