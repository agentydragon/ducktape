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

	"go.uber.org/zap"
)

// PeerReconcileInterval is how often we poll WireGuard for handshake times
// and potentially cycle endpoints.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go
const PeerReconcileInterval = 30 * time.Second

func main() {
	configPath := flag.String("config", "/etc/kubespan/agent.yaml", "path to config file")
	flag.Parse()

	logger, err := zap.NewProduction()
	if err != nil {
		fmt.Fprintf(os.Stderr, "logger: %v\n", err)
		os.Exit(1)
	}
	defer logger.Sync()

	if err := run(*configPath, logger); err != nil {
		logger.Fatal("kubespand exited with error", zap.Error(err))
	}
}

func run(configPath string, logger *zap.Logger) error {
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

	address, err := identity.ParsedAddress()
	if err != nil {
		return fmt.Errorf("parsing identity address: %w", err)
	}

	// Set up WireGuard interface.
	wg, err := NewWireGuardManager(identity.PrivateKey, cfg.ClusterSecret, cfg.ListenPort, cfg.MTU)
	if err != nil {
		return fmt.Errorf("wireguard manager: %w", err)
	}
	defer wg.Close()

	if err := wg.EnsureInterface(address); err != nil {
		return fmt.Errorf("wireguard interface: %w", err)
	}
	logger.Info("WireGuard interface ready", zap.String("interface", LinkName))

	// Set up routing and firewall.
	routing := NewRoutingManager(cfg.MTU)

	// Install initial (empty) routing rules — nftables with no routed IPs yet,
	// plus ip rules and default routes in table 180.
	if err := routing.Install(nil); err != nil {
		return fmt.Errorf("routing: %w", err)
	}
	logger.Info("routing rules installed",
		zap.Int("table", RoutingTable),
		zap.Int("rule_priority", RulePriority),
	)

	// Set up signal handler for cleanup.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

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
	if err := discovery.PublishLocal(identity, cfg.ListenPort); err != nil {
		return fmt.Errorf("publishing local identity: %w", err)
	}
	logger.Info("published to discovery service")

	// Main reconciliation loop.
	// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (Run loop)
	ticker := time.NewTicker(PeerReconcileInterval)
	defer ticker.Stop()

	peerStatuses := make(map[string]*PeerStatus)

	cleanup := func() {
		logger.Info("cleaning up")
		routing.Cleanup()
		wg.Cleanup()
	}

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
			_ = discovery.PublishLocal(identity, cfg.ListenPort)

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
		if forceRouting || (ps != nil && ps.State == PeerStateUp) {
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
				zap.String("state", string(ps.State)),
				zap.String("endpoint", ps.Endpoint.String()),
				zap.Time("last_handshake", ps.LastHandshakeTime),
			)
		}
	}

	return nil
}
