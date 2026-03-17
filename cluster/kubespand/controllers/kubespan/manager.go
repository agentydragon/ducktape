// ManagerController manages the KubeSpan WireGuard interface, routing rules,
// and peer state.
//
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
//
// # Upstream correspondence
//
// This controller now follows the upstream declarative COSI model:
// it writes network.LinkSpec (WireGuard config with embedded WireguardSpec),
// network.AddressSpec (ULA address), network.RouteSpec (routing table entries),
// and network.NfTablesChain as COSI resources. The WireguardLinkController
// applies the LinkSpec to the kernel, while AddressSpecController,
// RouteSpecController, and NfTablesChainController handle their respective
// resources.
//
// The only imperative operations remaining are:
//   - IP policy rules (fwmark → routing table) via RulesManager
//   - Read-only WireGuard device queries for handshake polling
//
// # What matches upstream
//
//   - Peer state machine: UpdateFromWireguard → CalculateState → ShouldChangeEndpoint
//     → PickNewEndpoint → UpdateEndpoint (identical adapter calls)
//   - NfTablesChain COSI resources: same chain names, rule structure, and verdict
//     patterns (mark matching, destination matching, MSS clamping)
//   - RulesManager for ip-rule fwmark→table routing (same interface, pulled from
//     upstream routing_rules.go)
//   - LinkSpec with WireguardSpec: peers as network.WireguardPeer (string keys,
//     string endpoints, netip.Prefix AllowedIPs) — matches upstream exactly
//   - Factory interfaces for WireguardClient and RulesManager (testability)
//   - IPSetBuilder for routed prefix compaction with ULA exclusion
//   - LinkSpec is always written on every reconciliation (idempotent via COSI)
//
// # What diverges
//
//   - No Enabled toggle: kubespand is always-on when running (no cfg.Enabled check).
//   - NfTablesChain outputs are OutputExclusive (kubespand is the only producer)
//     vs OutputShared in upstream (Talos has multiple chain producers).
//   - Nftables mark rules include explicit Xor:0 field for clarity.
package kubespanctrl

import (
	"context"
	"fmt"
	"net/netip"
	"slices"
	"time"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/siderolabs/talos/pkg/machinery/nethelpers"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"github.com/siderolabs/talos/pkg/machinery/resources/network"
	"go.uber.org/zap"
	"go4.org/netipx"
	"golang.zx2c4.com/wireguard/wgctrl"
	"golang.zx2c4.com/wireguard/wgctrl/wgtypes"

	"github.com/agentydragon/ducktape/cluster/kubespand/agentconfig"
	kubespanadapter "github.com/siderolabs/talos/internal/app/machined/pkg/adapters/kubespan"
	taloscontrollerskubespan "github.com/siderolabs/talos/internal/app/machined/pkg/controllers/kubespan"
)

// DefaultPeerReconcileInterval is how often we poll WireGuard for handshake
// times and potentially cycle endpoints.
// Ref: upstream DefaultPeerReconcileInterval
const DefaultPeerReconcileInterval = 30 * time.Second

// WireguardClient provides read-only access to WireGuard device state for
// handshake polling. Matches upstream Talos's WireguardClient interface.
// Ref: upstream WireguardClient (Device + Close)
type WireguardClient interface {
	Device(name string) (*wgtypes.Device, error)
	Close() error
}

// WireguardClientFactory creates a WireguardClient.
// Ref: upstream WireguardClientFactory
type WireguardClientFactory func() (WireguardClient, error)

// RulesManagerFactory creates a RulesManager.
// Ref: upstream RulesManagerFactory (identical signature)
type RulesManagerFactory func(targetTable uint8, internalMark, markMask uint32) taloscontrollerskubespan.RulesManager

// ManagerController manages the KubeSpan WireGuard interface, routing rules,
// and peer state. It watches Config, Identity, and PeerSpec resources, and
// produces PeerStatus, NfTablesChain, LinkSpec, AddressSpec, and RouteSpec
// resources.
type ManagerController struct {
	WireguardClientFactory WireguardClientFactory
	RulesManagerFactory    RulesManagerFactory
	PeerReconcileInterval  time.Duration

	wgClient WireguardClient
	rules    taloscontrollerskubespan.RulesManager
	ticker   *time.Ticker
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
		safe.Input[*agentconfig.Resource](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
// Ref: Talos manager.go Outputs()
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
		{
			Type: network.AddressSpecType,
			Kind: controller.OutputExclusive,
		},
		{
			Type: network.RouteSpecType,
			Kind: controller.OutputExclusive,
		},
		{
			Type: network.LinkSpecType,
			Kind: controller.OutputExclusive,
		},
	}
}

// Run implements controller.Controller.
func (ctrl *ManagerController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	if ctrl.WireguardClientFactory == nil {
		ctrl.WireguardClientFactory = func() (WireguardClient, error) {
			return wgctrl.New()
		}
	}
	if ctrl.RulesManagerFactory == nil {
		ctrl.RulesManagerFactory = taloscontrollerskubespan.NewRulesManager
	}
	if ctrl.PeerReconcileInterval == 0 {
		ctrl.PeerReconcileInterval = DefaultPeerReconcileInterval
	}

	ctrl.ticker = time.NewTicker(ctrl.PeerReconcileInterval)
	defer ctrl.ticker.Stop()

	// Only clean up on permanent shutdown (context cancellation), not on
	// transient reconcile errors. COSI restarts the controller after errors,
	// and the existing state should be reused.
	defer func() {
		if ctx.Err() != nil {
			ctrl.cleanup(logger)
		}
	}()

	// Timer-tick goroutine. When the ticker fires, we queue a reconcile
	// for peer status polling and state refresh.
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

	acfg, err := safe.ReaderGetByID[*agentconfig.Resource](ctx, r, agentconfig.ResourceID)
	if err != nil {
		if state.IsNotFoundError(err) {
			return nil
		}
		return fmt.Errorf("getting agent config: %w", err)
	}

	cfgSpec := cfg.TypedSpec()
	idSpec := id.TypedSpec()
	agentSpec := acfg.TypedSpec()

	// Initialize read-only WireGuard client and routing rules if needed.
	if ctrl.wgClient == nil {
		client, clientErr := ctrl.WireguardClientFactory()
		if clientErr != nil {
			return fmt.Errorf("wireguard client: %w", clientErr)
		}
		ctrl.wgClient = client

		// IP policy rules (fwmark → routing table).
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
		logger.Info("routing rules installed",
			zap.Int("table", constants.KubeSpanDefaultRoutingTable),
		)
	}

	// Write COSI AddressSpec for the ULA address on the kubespan interface.
	// Ref: Talos manager.go AddressSpec write
	if err := safe.WriterModify(ctx, r,
		network.NewAddressSpec(
			network.NamespaceName,
			network.AddressID(constants.KubeSpanLinkName, idSpec.Address),
		),
		func(res *network.AddressSpec) error {
			spec := res.TypedSpec()
			spec.Address = netip.PrefixFrom(idSpec.Address.Addr(), idSpec.Subnet.Bits())
			spec.ConfigLayer = network.ConfigOperator
			spec.Family = nethelpers.FamilyInet6
			spec.Flags = nethelpers.AddressFlags(nethelpers.AddressPermanent)
			spec.LinkName = constants.KubeSpanLinkName
			spec.Scope = nethelpers.ScopeGlobal
			return nil
		},
	); err != nil {
		return fmt.Errorf("error writing ULA address spec: %w", err)
	}

	// Write COSI RouteSpec for default routes in table 180.
	// Ref: Talos manager.go RouteSpec writes
	mtu := cfgSpec.MTU
	for _, routeSpec := range []network.RouteSpecSpec{
		{
			Family:      nethelpers.FamilyInet4,
			Destination: netip.Prefix{},
			Source:      netip.Addr{},
			Gateway:     netip.Addr{},
			MTU:         mtu,
			OutLinkName: constants.KubeSpanLinkName,
			Table:       nethelpers.RoutingTable(constants.KubeSpanDefaultRoutingTable),
			Priority:    1,
			Scope:       nethelpers.ScopeGlobal,
			Type:        nethelpers.TypeUnicast,
			Protocol:    nethelpers.ProtocolStatic,
			ConfigLayer: network.ConfigOperator,
		},
		{
			Family:      nethelpers.FamilyInet6,
			Destination: netip.Prefix{},
			Source:      netip.Addr{},
			Gateway:     netip.Addr{},
			MTU:         mtu,
			OutLinkName: constants.KubeSpanLinkName,
			Table:       nethelpers.RoutingTable(constants.KubeSpanDefaultRoutingTable),
			Priority:    1,
			Scope:       nethelpers.ScopeGlobal,
			Type:        nethelpers.TypeUnicast,
			Protocol:    nethelpers.ProtocolStatic,
			ConfigLayer: network.ConfigOperator,
		},
	} {
		if err := safe.WriterModify(ctx, r,
			network.NewRouteSpec(
				network.NamespaceName,
				network.RouteID(routeSpec.Table, routeSpec.Family, routeSpec.Destination, routeSpec.Gateway, routeSpec.Priority, routeSpec.OutLinkName),
			),
			func(res *network.RouteSpec) error {
				*res.TypedSpec() = routeSpec
				return nil
			},
		); err != nil {
			return fmt.Errorf("error writing route spec: %w", err)
		}
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

	// Poll WireGuard for peer state (read-only).
	var wgPeerList []wgtypes.Peer
	dev, handshakeErr := ctrl.wgClient.Device(constants.KubeSpanLinkName)
	if handshakeErr != nil {
		logger.Warn("failed to query WireGuard device", zap.Error(handshakeErr))
	} else {
		wgPeerList = dev.Peers
	}

	// Index WireGuard peers by public key for lookup.
	wgPeerMap := make(map[string]int, len(wgPeerList))
	for i, p := range wgPeerList {
		wgPeerMap[p.PublicKey.String()] = i
	}

	// Build WireGuard peer specs (network.WireguardPeer) and update statuses.
	var wgPeers []network.WireguardPeer

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
				zap.Int("endpoints", len(peerSpec.Endpoints)),
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

		// Build network.WireguardPeer (upstream type with string keys).
		wgPeer := network.WireguardPeer{
			PublicKey:                   pubKey,
			PresharedKey:                cfgSpec.SharedSecret,
			PersistentKeepaliveInterval: constants.KubeSpanDefaultPeerKeepalive,
			AllowedIPs:                  slices.Clone(peerSpec.AllowedIPs),
		}
		if ps.LastUsedEndpoint.IsValid() {
			wgPeer.Endpoint = ps.LastUsedEndpoint.String()
		} else if len(peerSpec.Endpoints) > 0 {
			wgPeer.Endpoint = peerSpec.Endpoints[0].String()
			kubespanadapter.PeerStatusSpec(ps).UpdateEndpoint(peerSpec.Endpoints[0])
		}
		wgPeers = append(wgPeers, wgPeer)

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

	// Write LinkSpec with embedded WireguardSpec.
	// Always written on every reconciliation — COSI's WriterModify is
	// idempotent (unchanged specs don't trigger downstream controllers),
	// and the WireguardLinkController's diff-based Encode() avoids
	// kernel writes when the config hasn't changed.
	if err := safe.WriterModify(ctx, r,
		network.NewLinkSpec(network.NamespaceName, network.LinkID(constants.KubeSpanLinkName)),
		func(res *network.LinkSpec) error {
			spec := res.TypedSpec()
			spec.Name = constants.KubeSpanLinkName
			spec.Type = nethelpers.LinkNone
			spec.Kind = network.LinkKindWireguard
			spec.Up = true
			spec.Logical = true
			spec.MTU = cfgSpec.MTU
			spec.ConfigLayer = network.ConfigOperator
			spec.Wireguard = network.WireguardSpec{
				PrivateKey:   idSpec.PrivateKey,
				ListenPort:   agentSpec.ListenPort,
				FirewallMark: constants.KubeSpanDefaultFirewallMark,
				Peers:        wgPeers,
			}
			return nil
		},
	); err != nil {
		return fmt.Errorf("writing link spec: %w", err)
	}

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

// cleanup tears down routing rules and releases the WireGuard client.
// WireGuard interface cleanup is handled by WireguardLinkController
// when the LinkSpec resource is torn down.
func (ctrl *ManagerController) cleanup(logger *zap.Logger) {
	if ctrl.rules != nil {
		if err := ctrl.rules.Cleanup(); err != nil {
			logger.Error("ip rules cleanup failed", zap.Error(err))
		}
		ctrl.rules = nil
	}
	if ctrl.wgClient != nil {
		ctrl.wgClient.Close()
		ctrl.wgClient = nil
	}
}
