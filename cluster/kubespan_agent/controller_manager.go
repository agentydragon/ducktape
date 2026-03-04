// ManagerController manages the KubeSpan WireGuard interface, routing rules,
// and peer state.
//
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
//
// # Upstream correspondence
//
// The upstream Talos ManagerController uses a fully declarative COSI model:
// it writes network.LinkSpec (WireGuard config), network.AddressSpec (ULA
// address), and network.RouteSpec (routing table entries) as COSI resources,
// which are then applied by Talos's LinkSpecController, AddressSpecController,
// and RouteSpecController respectively. It only reads WireGuard device state
// (via WireguardClient.Device) for handshake polling.
//
// Kubespand cannot adopt that model without also pulling in the entire Talos
// network operator stack, so this controller is intentionally imperative for
// the WireGuard/netlink side: it calls wgctrl and netlink directly to create
// the interface, configure peers, and install routes.
//
// # What matches upstream
//
//   - Peer state machine: UpdateFromWireguard → CalculateState → ShouldChangeEndpoint
//     → PickNewEndpoint → UpdateEndpoint (identical adapter calls)
//   - NfTablesChain COSI resources: same chain names, rule structure, and verdict
//     patterns (mark matching, destination matching, MSS clamping)
//   - RulesManager for ip-rule fwmark→table routing (same interface, pulled from
//     upstream routing_rules.go)
//   - Factory interfaces for WireguardClient and RulesManager (testability)
//   - IPSetBuilder for routed prefix compaction with ULA exclusion
//   - updateSpecs optimization: skip WireGuard reconfiguration on timer-only ticks
//
// # What diverges
//
//   - Kubespand calls wgctrl.ConfigureDevice directly instead of writing
//     network.LinkSpec with WireguardSpec. Upstream WireguardClient is read-only
//     (Device+Close); kubespand's WireguardManager also writes (EnsureInterface,
//     ConfigurePeers, Cleanup).
//   - Kubespand calls netlink directly for routes (routing.InstallRoutes) and
//     interface creation (wireguard.Manager.EnsureInterface) instead of writing
//     network.RouteSpec/AddressSpec COSI resources.
//   - No Enabled toggle: kubespand is always-on when running (no cfg.Enabled check).
//   - NfTablesChain outputs are OutputExclusive (kubespand is the only producer)
//     vs OutputShared in upstream (Talos has multiple chain producers).
//   - Nftables mark rules include explicit Xor:0 field for clarity.
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
	"go4.org/netipx"
	"golang.zx2c4.com/wireguard/wgctrl/wgtypes"

	kubespanadapter "github.com/agentydragon/ducktape/cluster/kubespan_agent/peerstate"
	routing "github.com/agentydragon/ducktape/cluster/kubespan_agent/routing"
	"github.com/agentydragon/ducktape/cluster/kubespan_agent/wireguard"
)

// DefaultPeerReconcileInterval is how often we poll WireGuard for handshake
// times and potentially cycle endpoints.
// Ref: upstream DefaultPeerReconcileInterval
const DefaultPeerReconcileInterval = 30 * time.Second

// WireguardManager abstracts WireGuard interface management for testability.
// Upstream Talos uses a read-only WireguardClient (Device+Close) because it
// writes config via COSI LinkSpec. Kubespand manages the interface imperatively,
// so this interface includes both read and write operations.
type WireguardManager interface {
	EnsureInterface(address netip.Prefix) error
	PresharedKey() *wgtypes.Key
	ConfigurePeers(peers []wgtypes.PeerConfig) error
	GetPeers() ([]wgtypes.Peer, error)
	Cleanup() error
	Close() error
}

// WireguardManagerFactory creates a WireguardManager.
// Ref: upstream WireguardClientFactory (but wraps read+write, not read-only)
type WireguardManagerFactory func(privateKey string, psk string, listenPort, mtu int) (WireguardManager, error)

// RulesManagerFactory creates a RulesManager.
// Ref: upstream RulesManagerFactory (identical signature)
type RulesManagerFactory func(targetTable uint8, internalMark, markMask uint32) routing.RulesManager

// ManagerController manages the KubeSpan WireGuard interface, routing rules,
// and peer state. It watches Config, Identity, and PeerSpec resources, and
// produces PeerStatus and NfTablesChain resources.
type ManagerController struct {
	WireguardManagerFactory WireguardManagerFactory
	RulesManagerFactory     RulesManagerFactory
	PeerReconcileInterval   time.Duration

	wg     WireguardManager
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
// NfTablesChain is OutputExclusive because kubespand is the sole producer
// (upstream uses OutputShared since Talos has multiple chain producers).
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
	if ctrl.WireguardManagerFactory == nil {
		ctrl.WireguardManagerFactory = func(privateKey string, psk string, listenPort, mtu int) (WireguardManager, error) {
			return wireguard.NewManager(privateKey, psk, listenPort, mtu)
		}
	}
	if ctrl.RulesManagerFactory == nil {
		ctrl.RulesManagerFactory = routing.NewRulesManager
	}
	if ctrl.PeerReconcileInterval == 0 {
		ctrl.PeerReconcileInterval = DefaultPeerReconcileInterval
	}

	ctrl.ticker = time.NewTicker(ctrl.PeerReconcileInterval)
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

	// Timer-tick goroutine. When the ticker fires, we queue a reconcile
	// but set updateSpecs=false (only peer status polling, no WireGuard
	// reconfiguration needed unless an endpoint actually changes).
	// Ref: upstream tickerC / updateSpecs pattern
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

	var updateSpecs bool

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
			updateSpecs = true
		}

		if err := ctrl.reconcile(ctx, r, logger, &updateSpecs); err != nil {
			return err
		}
	}
}

func (ctrl *ManagerController) reconcile(ctx context.Context, r controller.Runtime, logger *zap.Logger, updateSpecs *bool) error {
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
		wg, wgErr := ctrl.WireguardManagerFactory(idSpec.PrivateKey, cfgSpec.SharedSecret, agentCfg.ListenPort, int(cfgSpec.MTU))
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
		ctrl.rules = ctrl.RulesManagerFactory(
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

	// Build routed IP set using IPSetBuilder for prefix compaction.
	// Upstream excludes KubeSpan ULA addresses from the routed set since
	// those are handled by the WireGuard interface directly.
	// Ref: upstream routedIPsBuilder
	var routedIPsBuilder netipx.IPSetBuilder

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
				*updateSpecs = true
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
			*updateSpecs = true
		}
		wgPeers = append(wgPeers, peerCfg)

		// Collect routed prefixes for nftables, excluding KubeSpan ULA addresses.
		// Ref: upstream routedIPsBuilder with network.IsULA filter
		if cfgSpec.ForceRouting || ps.State == kubespan.PeerStateUp {
			for _, prefix := range peerSpec.AllowedIPs {
				if !network.IsULA(prefix.Addr(), network.ULAKubeSpan) {
					routedIPsBuilder.AddPrefix(prefix)
				}
			}
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

	routedIPsSet, err := routedIPsBuilder.IPSet()
	if err != nil {
		return fmt.Errorf("building routed IPs set: %w", err)
	}

	// Always update nftables — the routed IP set may change due to peer
	// state transitions even on timer-only ticks.
	if err := ctrl.writeNfTablesChains(ctx, r, routedIPsSet.Prefixes(), cfgSpec.MTU); err != nil {
		return fmt.Errorf("writing nftables chains: %w", err)
	}

	if !*updateSpecs {
		// Micro-optimization: skip WireGuard reconfiguration when only
		// a timer tick fired and no endpoint changes occurred.
		// Ref: upstream updateSpecs optimization
		r.ResetRestartBackoff()
		return nil
	}

	if err := ctrl.wg.ConfigurePeers(wgPeers); err != nil {
		return fmt.Errorf("configuring WireGuard peers: %w", err)
	}

	*updateSpecs = false
	r.ResetRestartBackoff()
	return nil
}

// writeNfTablesChains writes NfTablesChain COSI resources for the prerouting
// and output chains. The NfTablesChainController watches these and applies
// them to the kernel nftables subsystem.
//
// Rule structure matches upstream exactly. Prefixes are pre-compacted by
// IPSetBuilder, so overlapping ranges are merged before being written.
func (ctrl *ManagerController) writeNfTablesChains(
	ctx context.Context,
	r controller.Runtime,
	routedPrefixes []netip.Prefix,
	mtu uint32,
) error {
	// Prerouting chain: mark incoming packets for routed prefixes.
	// Ref: upstream kubespan_prerouting chain
	if err := safe.WriterModify(ctx, r,
		network.NewNfTablesChain(network.NamespaceName, "kubespan_prerouting"),
		func(chain *network.NfTablesChain) error {
			spec := chain.TypedSpec()
			spec.Type = nethelpers.ChainTypeFilter
			spec.Hook = nethelpers.ChainHookPrerouting
			spec.Priority = nethelpers.ChainPriorityFilter
			spec.Policy = nethelpers.VerdictAccept
			spec.Rules = buildPreroutingRules(routedPrefixes)
			return nil
		},
	); err != nil {
		return fmt.Errorf("prerouting chain: %w", err)
	}

	// Output chain: mark outgoing packets, clamp MSS.
	// Ref: upstream kubespan_outgoing chain
	if err := safe.WriterModify(ctx, r,
		network.NewNfTablesChain(network.NamespaceName, "kubespan_outgoing"),
		func(chain *network.NfTablesChain) error {
			spec := chain.TypedSpec()
			spec.Type = nethelpers.ChainTypeRoute
			spec.Hook = nethelpers.ChainHookOutput
			spec.Priority = nethelpers.ChainPriorityFilter
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
// Ref: upstream kubespan_prerouting rules
func buildPreroutingRules(routedPrefixes []netip.Prefix) []network.NfTablesRule {
	acceptVerdict := nethelpers.VerdictAccept

	rules := []network.NfTablesRule{
		// Skip packets already marked by WireGuard (mark & mask == fwmark → accept).
		{
			MatchMark: &network.NfTablesMark{
				Mask:  constants.KubeSpanDefaultFirewallMask,
				Value: constants.KubeSpanDefaultFirewallMark,
			},
			Verdict: &acceptVerdict,
		},
	}

	if len(routedPrefixes) > 0 {
		// Mark destination-matched packets with force mark.
		rules = append(rules, network.NfTablesRule{
			MatchDestinationAddress: &network.NfTablesAddressMatch{
				IncludeSubnets: routedPrefixes,
			},
			SetMark: &network.NfTablesMark{
				Mask: ^uint32(constants.KubeSpanDefaultFirewallMask),
				Xor:  constants.KubeSpanDefaultForceFirewallMark,
			},
			Verdict: &acceptVerdict,
		})
	}

	return rules
}

// buildOutputRules produces NfTablesRule specs for the output chain.
// Ref: upstream kubespan_outgoing rules
func buildOutputRules(routedPrefixes []netip.Prefix, mtu uint16) []network.NfTablesRule {
	acceptVerdict := nethelpers.VerdictAccept

	rules := []network.NfTablesRule{
		// Skip packets already marked by WireGuard.
		{
			MatchMark: &network.NfTablesMark{
				Mask:  constants.KubeSpanDefaultFirewallMask,
				Value: constants.KubeSpanDefaultFirewallMark,
			},
			Verdict: &acceptVerdict,
		},
		// Skip loopback traffic.
		{
			MatchOIfName: &network.NfTablesIfNameMatch{
				InterfaceNames: []string{"lo"},
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
				Mask: ^uint32(constants.KubeSpanDefaultFirewallMask),
				Xor:  constants.KubeSpanDefaultForceFirewallMark,
			},
			Verdict: &acceptVerdict,
		})
	}

	return rules
}

// cleanup tears down the WireGuard interface and routing rules.
// Divergence: upstream cleans up by destroying owned COSI resources
// (LinkSpec/AddressSpec/RouteSpec/NfTablesChain/PeerStatus). Kubespand
// calls netlink/wgctrl directly since it manages the interface imperatively.
// Nftables cleanup is handled by NfTablesChainController when its COSI
// resources are torn down.
func (ctrl *ManagerController) cleanup(logger *zap.Logger) {
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
