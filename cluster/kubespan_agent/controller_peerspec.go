package main

import (
	"context"
	"fmt"
	"net/netip"
	"strings"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/resources/cluster"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"go.uber.org/zap"
	"go4.org/netipx"
)

// peerData holds a peer's spec and computed IP set for overlap detection.
type peerData struct {
	pubKey    string
	spec      kubespan.PeerSpecSpec
	allowedIP *netipx.IPSet
}

// PeerSpecController watches cluster.Affiliate + Config + Identity and produces
// kubespan.PeerSpec resources, with endpoint filtering and IP overlap detection.
//
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/peer_spec.go
type PeerSpecController struct{}

// Name implements controller.Controller.
func (ctrl *PeerSpecController) Name() string {
	return "kubespan.PeerSpecController"
}

// Inputs implements controller.Controller.
func (ctrl *PeerSpecController) Inputs() []controller.Input {
	return []controller.Input{
		safe.Input[*kubespan.Config](controller.InputWeak),
		safe.Input[*kubespan.Identity](controller.InputWeak),
		safe.Input[*cluster.Affiliate](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *PeerSpecController) Outputs() []controller.Output {
	return []controller.Output{
		{
			Type: kubespan.PeerSpecType,
			Kind: controller.OutputExclusive,
		},
	}
}

// Run implements controller.Controller.
func (ctrl *PeerSpecController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
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

		id, err := safe.ReaderGetByID[*kubespan.Identity](ctx, r, kubespan.LocalIdentity)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}
			return fmt.Errorf("getting identity: %w", err)
		}

		cfgSpec := cfg.TypedSpec()
		idSpec := id.TypedSpec()

		affiliates, err := safe.ReaderListAll[*cluster.Affiliate](ctx, r)
		if err != nil {
			return fmt.Errorf("listing affiliates: %w", err)
		}

		filters := parseEndpointFilters(cfgSpec.EndpointFilters)

		// Build PeerSpec for each affiliate (skip self).
		var peers []peerData

		for aff := range affiliates.All() {
			affSpec := aff.TypedSpec()
			ks := affSpec.KubeSpan

			if ks.PublicKey == "" || ks.PublicKey == idSpec.PublicKey {
				continue
			}

			// Build AllowedIPs using IPSetBuilder so we can subtract ExcludeAdvertisedNetworks.
			// Ref: talos/internal/app/machined/pkg/controllers/kubespan/peer_spec.go (ipSetForPeer)
			var builder netipx.IPSetBuilder

			for _, p := range ks.AdditionalAddresses {
				builder.AddPrefix(p)
			}
			for _, addr := range affSpec.Addresses {
				builder.Add(addr)
			}
			for _, p := range ks.ExcludeAdvertisedNetworks {
				builder.RemovePrefix(p)
			}
			// KubeSpan address is always included (added after exclusions).
			if ks.Address.IsValid() {
				builder.Add(ks.Address)
			}

			allowedIPSet, buildErr := builder.IPSet()
			if buildErr != nil {
				logger.Warn("failed to build IP set for peer", zap.String("peer", ks.PublicKey), zap.Error(buildErr))
				continue
			}
			allowedIPs := allowedIPSet.Prefixes()

			// Filter endpoints.
			var endpoints []netip.AddrPort
			for _, ep := range ks.Endpoints {
				if endpointAllowed(ep, filters) {
					endpoints = append(endpoints, ep)
				}
			}

			spec := kubespan.PeerSpecSpec{
				Address:    ks.Address,
				AllowedIPs: allowedIPs,
				Endpoints:  endpoints,
				Label:      affSpec.Hostname,
			}

			ipSet := buildIPSet(allowedIPs)
			peers = append(peers, peerData{pubKey: ks.PublicKey, spec: spec, allowedIP: ipSet})
		}

		// Detect and resolve IP overlaps between peers.
		// Ref: talos/internal/app/machined/pkg/controllers/kubespan/peer_spec.go (ipSetForPeer)
		resolveOverlaps(peers, logger)

		r.StartTrackingOutputs()

		for _, p := range peers {
			if err := safe.WriterModify(ctx, r,
				kubespan.NewPeerSpec(kubespan.NamespaceName, resource.ID(p.pubKey)),
				func(res *kubespan.PeerSpec) error {
					*res.TypedSpec() = p.spec
					return nil
				},
			); err != nil {
				return fmt.Errorf("writing peer spec %s: %w", p.spec.Label, err)
			}
		}

		if err := safe.CleanupOutputs[*kubespan.PeerSpec](ctx, r); err != nil {
			return fmt.Errorf("cleaning up peer specs: %w", err)
		}

		logger.Debug("peerspec reconciled", zap.Int("peers", len(peers)))
		r.ResetRestartBackoff()
	}
}

// endpointFilter is a parsed CIDR filter with allow/deny semantics.
type endpointFilter struct {
	prefix netip.Prefix
	deny   bool
}

// parseEndpointFilters parses endpoint filter strings ("!cidr" for deny, "cidr" for allow).
func parseEndpointFilters(raw []string) []endpointFilter {
	var filters []endpointFilter
	for _, s := range raw {
		deny := false
		cidr := s
		if strings.HasPrefix(s, "!") {
			deny = true
			cidr = s[1:]
		}
		prefix, err := netip.ParsePrefix(cidr)
		if err != nil {
			continue
		}
		filters = append(filters, endpointFilter{prefix: prefix, deny: deny})
	}
	return filters
}

// endpointAllowed checks if an endpoint is allowed by the filter list.
// First match wins. Empty filters = allow all.
func endpointAllowed(ep netip.AddrPort, filters []endpointFilter) bool {
	if len(filters) == 0 {
		return true
	}
	for _, f := range filters {
		if f.prefix.Contains(ep.Addr()) {
			return !f.deny
		}
	}
	return false
}

// buildIPSet creates an IPSet from a list of prefixes for overlap detection.
func buildIPSet(prefixes []netip.Prefix) *netipx.IPSet {
	var b netipx.IPSetBuilder
	for _, p := range prefixes {
		b.AddPrefix(p)
	}
	set, _ := b.IPSet()
	return set
}

// resolveOverlaps detects and resolves IP overlaps between peers.
// The peer whose KubeSpan address falls within a prefix "owns" it;
// the prefix is removed from the other peer.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/peer_spec.go
func resolveOverlaps(peers []peerData, logger *zap.Logger) {
	for i := range peers {
		for j := i + 1; j < len(peers); j++ {
			if peers[i].allowedIP == nil || peers[j].allowedIP == nil {
				continue
			}

			// Check each prefix in peer j against peer i's set.
			peers[j].spec.AllowedIPs = filterOverlapping(
				peers[j].spec.AllowedIPs, peers[i].allowedIP,
				peers[j].spec.Address, peers[i].spec.Label, peers[j].spec.Label, logger,
			)

			// Check each prefix in peer i against peer j's set.
			peers[i].spec.AllowedIPs = filterOverlapping(
				peers[i].spec.AllowedIPs, peers[j].allowedIP,
				peers[i].spec.Address, peers[j].spec.Label, peers[i].spec.Label, logger,
			)

			// Rebuild IP sets after modification.
			peers[i].allowedIP = buildIPSet(peers[i].spec.AllowedIPs)
			peers[j].allowedIP = buildIPSet(peers[j].spec.AllowedIPs)
		}
	}
}

// filterOverlapping removes prefixes from candidate that overlap with otherSet,
// unless the candidateAddr falls within the prefix (meaning candidate "owns" it).
func filterOverlapping(candidate []netip.Prefix, otherSet *netipx.IPSet, candidateAddr netip.Addr, otherLabel, candidateLabel string, logger *zap.Logger) []netip.Prefix {
	var result []netip.Prefix
	for _, p := range candidate {
		if !prefixOverlapsSet(p, otherSet) {
			result = append(result, p)
			continue
		}

		// Candidate's own address falls within the prefix — candidate owns it.
		if candidateAddr.IsValid() && p.Contains(candidateAddr) {
			result = append(result, p)
			continue
		}

		logger.Warn("removing overlapping prefix",
			zap.Stringer("prefix", p),
			zap.String("removed_from", candidateLabel),
			zap.String("overlaps_with", otherLabel),
		)
	}
	return result
}

// prefixOverlapsSet checks whether any IP in the prefix is contained in the set.
func prefixOverlapsSet(p netip.Prefix, set *netipx.IPSet) bool {
	return set.OverlapsPrefix(p)
}
