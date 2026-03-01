package main

import (
	"context"
	"flag"
	"fmt"
	"net/netip"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"go.uber.org/zap"
)

// PeerReconcileInterval is how often we poll WireGuard for handshake times
// and potentially cycle endpoints.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
const PeerReconcileInterval = 30 * time.Second

func main() {
	configPath := flag.String("config", "/etc/kubespan/agent.yaml", "path to config file")
	discoveryOnly := flag.Bool("discovery-only", false, "run discovery only (no WireGuard/routing), exit when peers found")
	discoveryTimeout := flag.Duration("timeout", 0, "timeout for discovery-only mode (0 = no timeout)")
	flag.Parse()

	logger, err := zap.NewProduction()
	if err != nil {
		fmt.Fprintf(os.Stderr, "logger: %v\n", err)
		os.Exit(1)
	}
	defer logger.Sync()

	if err := run(*configPath, *discoveryOnly, *discoveryTimeout, logger); err != nil {
		logger.Fatal("kubespand exited with error", zap.Error(err))
	}
}

func run(configPath string, discoveryOnly bool, discoveryTimeout time.Duration, logger *zap.Logger) error {
	// Load config.
	cfg, err := LoadConfig(configPath)
	if err != nil {
		return fmt.Errorf("config: %w", err)
	}
	logger.Info("loaded config",
		zap.String("cluster_id", cfg.ClusterID),
		zap.String("discovery_endpoint", cfg.DiscoveryEndpoint),
		zap.Int("listen_port", cfg.ListenPort),
		zap.Int("mtu", cfg.MTU),
		zap.Bool("discovery_only", discoveryOnly),
	)

	// Load or create identity.
	mac, err := DetectMAC()
	if err != nil {
		return fmt.Errorf("detecting MAC: %w", err)
	}
	logger.Info("detected MAC", zap.String("mac", mac.String()))

	identity, err := LoadOrCreateIdentity(cfg.IdentityFile, cfg.ClusterID)
	if err != nil {
		return fmt.Errorf("identity: %w", err)
	}

	if err := identity.UpdateAddress(cfg.ClusterID, mac); err != nil {
		return fmt.Errorf("computing address: %w", err)
	}
	logger.Info("identity ready",
		zap.String("public_key", identity.PublicKey),
		zap.String("subnet", identity.Subnet),
		zap.String("address", identity.Address),
	)

	// Set up WireGuard and routing (skip in discovery-only mode).
	var wg *WireGuardManager
	var routing *RoutingManager

	if !discoveryOnly {
		address, err := identity.ParsedAddress()
		if err != nil {
			return fmt.Errorf("parsing identity address: %w", err)
		}

		wg, err = NewWireGuardManager(identity.PrivateKey, cfg.ClusterSecret, cfg.ListenPort, cfg.MTU)
		if err != nil {
			return fmt.Errorf("wireguard manager: %w", err)
		}
		defer wg.Close()

		if err := wg.EnsureInterface(address); err != nil {
			return fmt.Errorf("wireguard interface: %w", err)
		}
		logger.Info("WireGuard interface ready", zap.String("interface", constants.KubeSpanLinkName))

		routing = NewRoutingManager(cfg.MTU)
		if err := routing.Install(nil); err != nil {
			return fmt.Errorf("routing: %w", err)
		}
		logger.Info("routing rules installed",
			zap.Int("table", constants.KubeSpanDefaultRoutingTable),
			zap.Int("rule_priority", RulePriority),
		)
	}

	// Set up context with optional timeout for discovery-only mode.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	if discoveryOnly && discoveryTimeout > 0 {
		ctx, cancel = context.WithTimeout(ctx, discoveryTimeout)
		defer cancel()
	}

	// Set up signal handler.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)

	// Start discovery client.
	discovery, err := NewDiscoveryManager(cfg, identity.PublicKey, logger)
	if err != nil {
		return fmt.Errorf("discovery manager: %w", err)
	}

	discoveryErrCh := make(chan error, 1)
	go func() {
		discoveryErrCh <- discovery.Run(ctx)
	}()

	// Publish our identity to the discovery service.
	if err := discovery.PublishLocal(cfg, identity, cfg.ListenPort); err != nil {
		return fmt.Errorf("publishing local identity: %w", err)
	}
	logger.Info("published to discovery service")

	cleanup := func() {
		logger.Info("cleaning up")
		discovery.DeleteLocalAffiliate()
		if routing != nil {
			routing.Cleanup()
		}
		if wg != nil {
			wg.Cleanup()
		}
	}

	if discoveryOnly {
		return runDiscoveryLoop(ctx, discovery, cfg, identity, cleanup, discoveryErrCh, logger)
	}
	return runFullLoop(ctx, cancel, wg, routing, discovery, cfg, identity, sigCh, discoveryErrCh, cleanup, logger)
}

// runDiscoveryLoop runs a discovery-only event loop: waits for peers then exits.
func runDiscoveryLoop(
	ctx context.Context,
	discovery *DiscoveryManager,
	cfg *Config,
	identity *Identity,
	cleanup func(),
	discoveryErrCh <-chan error,
	logger *zap.Logger,
) error {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()

	checkPeers := func() bool {
		peers := discovery.GetPeers()
		if len(peers) == 0 {
			return false
		}
		for _, p := range peers {
			logger.Info("discovered peer",
				zap.String("label", p.Label),
				zap.String("public_key", p.PublicKey),
				zap.String("address", p.Address.String()),
				zap.Int("endpoints", len(p.Endpoints)),
			)
		}
		logger.Info("discovery-only mode: peers found, exiting successfully", zap.Int("count", len(peers)))
		return true
	}

	for {
		select {
		case err := <-discoveryErrCh:
			cleanup()
			if err != nil {
				return fmt.Errorf("discovery client: %w", err)
			}
			return fmt.Errorf("discovery client exited without finding peers")

		case <-ctx.Done():
			cleanup()
			return fmt.Errorf("timeout waiting for peers")

		case <-discovery.NotifyCh():
			if checkPeers() {
				cleanup()
				return nil
			}

		case <-ticker.C:
			if checkPeers() {
				cleanup()
				return nil
			}
			// Re-publish to keep TTL fresh.
			_ = discovery.PublishLocal(cfg, identity, cfg.ListenPort)
		}
	}
}

// runFullLoop runs the full WireGuard reconciliation event loop.
func runFullLoop(
	ctx context.Context,
	cancel context.CancelFunc,
	wg *WireGuardManager,
	routing *RoutingManager,
	discovery *DiscoveryManager,
	cfg *Config,
	identity *Identity,
	sigCh <-chan os.Signal,
	discoveryErrCh <-chan error,
	cleanup func(),
	logger *zap.Logger,
) error {
	// Main reconciliation loop.
	// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (Run loop)
	ticker := time.NewTicker(PeerReconcileInterval)
	defer ticker.Stop()

	peerStatuses := make(map[string]*PeerStatus)

	for {
		select {
		case sig := <-sigCh:
			logger.Info("received signal, shutting down", zap.String("signal", sig.String()))
			cancel()
			cleanup()
			return nil

		case err := <-discoveryErrCh:
			cleanup()
			if err != nil {
				return fmt.Errorf("discovery client: %w", err)
			}
			return nil

		case <-discovery.NotifyCh():
			// Peer list changed — reconcile.
			peers := discovery.GetPeers()
			logger.Info("discovery update", zap.Int("peers", len(peers)))
			if err := reconcilePeers(wg, routing, peers, peerStatuses, cfg.ForceRouting, logger); err != nil {
				logger.Error("reconcile failed", zap.Error(err))
			}
			// Re-publish to keep TTL fresh.
			_ = discovery.PublishLocal(cfg, identity, cfg.ListenPort)

		case <-ticker.C:
			// Periodic reconciliation — check handshakes, cycle endpoints if needed.
			peers := discovery.GetPeers()
			if err := reconcilePeers(wg, routing, peers, peerStatuses, cfg.ForceRouting, logger); err != nil {
				logger.Error("periodic reconcile failed", zap.Error(err))
			}
		}
	}
}

// reconcilePeers syncs the WireGuard peer config with discovery data, updates
// peer states from WireGuard handshake info, and cycles endpoints for down peers.
//
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
//
//	(the main loop body: poll handshakes → calculate state → cycle endpoints →
//	 build WG peers → update nftables)
func reconcilePeers(
	wg *WireGuardManager,
	routing *RoutingManager,
	peers []Peer,
	statuses map[string]*PeerStatus,
	forceRouting bool,
	logger *zap.Logger,
) error {
	// Build peer map.
	peerMap := make(map[string]*Peer, len(peers))
	for i := range peers {
		peerMap[peers[i].PublicKey] = &peers[i]
	}

	// Remove statuses for peers that are gone.
	for key := range statuses {
		if _, ok := peerMap[key]; !ok {
			delete(statuses, key)
		}
	}

	// Ensure statuses exist for all peers.
	for key, peer := range peerMap {
		if _, ok := statuses[key]; !ok {
			statuses[key] = &PeerStatus{Label: peer.Label}
		}
	}

	// Poll WireGuard for handshake info.
	// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (UpdateFromWireguard)
	handshakes, err := wg.GetPeerHandshakes()
	if err != nil {
		logger.Warn("failed to query WireGuard handshakes", zap.Error(err))
	} else {
		for key, info := range handshakes {
			if ps, ok := statuses[key]; ok {
				ps.LastHandshakeTime = info.LastHandshakeTime
				ps.Endpoint = info.Endpoint
				ps.TransmitBytes = info.TransmitBytes
				ps.ReceiveBytes = info.ReceiveBytes
			}
		}
	}

	// Calculate peer states and cycle endpoints if needed.
	for key, ps := range statuses {
		ps.CalculateState()

		if ps.ShouldChangeEndpoint() {
			if peer, ok := peerMap[key]; ok {
				newEP := ps.PickNewEndpoint(peer.Endpoints)
				if newEP.IsValid() {
					logger.Info("cycling endpoint",
						zap.String("peer", ps.Label),
						zap.String("old", ps.LastUsedEndpoint.String()),
						zap.String("new", newEP.String()),
					)
					ps.UpdateEndpoint(newEP)
				}
			}
		}
	}

	// Build WireGuard peer configs.
	wgPeers := make([]WireGuardPeer, 0, len(peers))
	var routedPrefixes []netip.Prefix

	for _, peer := range peers {
		ps := statuses[peer.PublicKey]

		wgPeer := WireGuardPeer{
			PublicKey:  peer.PublicKey,
			AllowedIPs: peer.AllowedIPs,
		}
		if ps != nil && ps.LastUsedEndpoint.IsValid() {
			wgPeer.Endpoint = ps.LastUsedEndpoint
		} else if len(peer.Endpoints) > 0 {
			wgPeer.Endpoint = peer.Endpoints[0]
			if ps != nil {
				ps.UpdateEndpoint(peer.Endpoints[0])
			}
		}

		wgPeers = append(wgPeers, wgPeer)

		// Collect routed prefixes for nftables.
		// Only route through peers that are UP (or all if force_routing).
		// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (routedPeersIPs)
		if forceRouting || (ps != nil && ps.State == kubespan.PeerStateUp) {
			routedPrefixes = append(routedPrefixes, peer.AllowedIPs...)
		}
	}

	// Update WireGuard peers.
	if err := wg.ConfigurePeers(wgPeers); err != nil {
		return fmt.Errorf("configuring WireGuard peers: %w", err)
	}

	// Update nftables routed prefix sets.
	if err := routing.Update(routedPrefixes); err != nil {
		return fmt.Errorf("updating nftables: %w", err)
	}

	// Log peer summary.
	for key, ps := range statuses {
		if peer, ok := peerMap[key]; ok {
			logger.Debug("peer status",
				zap.String("label", peer.Label),
				zap.String("state", ps.State.String()),
				zap.String("endpoint", ps.Endpoint.String()),
				zap.Time("last_handshake", ps.LastHandshakeTime),
			)
		}
	}

	return nil
}
