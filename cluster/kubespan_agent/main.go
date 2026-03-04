package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	controllerruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/cluster/kubespan_agent/agentconfig"
	endpoint "github.com/agentydragon/ducktape/cluster/kubespan_agent/endpoint"
	networkadapter "github.com/agentydragon/ducktape/cluster/kubespan_agent/nftables"
	peerspec "github.com/agentydragon/ducktape/cluster/kubespan_agent/peerspec"
)

// agentCfg is the parsed agent configuration, accessible to controllers
// for agent-specific fields not in upstream kubespan.ConfigSpec.
var agentCfg *agentconfig.AgentConfig

func main() {
	configPath := flag.String("config", "/etc/kubespan/agent.yaml", "path to config file")
	discoveryOnly := flag.Bool("discovery-only", false, "run discovery only (no WireGuard/routing), exit when peers found")
	discoveryTimeout := flag.Duration("timeout", 0, "timeout for discovery-only mode (0 = no timeout)")
	debug := flag.Bool("debug", false, "enable debug logging")
	flag.Parse()

	var logger *zap.Logger
	var err error
	if *debug {
		logger, err = zap.NewDevelopment()
	} else {
		logger, err = zap.NewProduction()
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "logger: %v\n", err)
		os.Exit(1)
	}
	defer logger.Sync() //nolint:errcheck

	if err := run(*configPath, *discoveryOnly, *discoveryTimeout, logger); err != nil {
		logger.Fatal("kubespand exited with error", zap.Error(err))
	}
}

func run(configPath string, discoveryOnly bool, discoveryTimeout time.Duration, logger *zap.Logger) error {
	// Load agent config from YAML.
	var err error
	agentCfg, err = agentconfig.Load(configPath)
	if err != nil {
		return fmt.Errorf("config: %w", err)
	}
	logger.Info("loaded config",
		zap.String("cluster_id", agentCfg.ClusterID),
		zap.String("discovery_endpoint", agentCfg.DiscoveryEndpoint),
		zap.Int("listen_port", agentCfg.ListenPort),
		zap.Uint32("mtu", agentCfg.MTU),
		zap.Bool("discovery_only", discoveryOnly),
	)

	// Convert to upstream ConfigSpec for COSI injection.
	cfgSpec := agentCfg.ToConfigSpec()

	// Create COSI in-memory state.
	st := state.WrapCore(namespaced.NewState(inmem.Build))

	// Set up context with signal handling.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	if discoveryOnly && discoveryTimeout > 0 {
		ctx, cancel = context.WithTimeout(ctx, discoveryTimeout)
		defer cancel()
	}

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		select {
		case sig := <-sigCh:
			logger.Info("received signal, shutting down", zap.String("signal", sig.String()))
			cancel()
		case <-ctx.Done():
		}
	}()

	// Create COSI controller runtime.
	rt, err := controllerruntime.NewRuntime(st, logger)
	if err != nil {
		return fmt.Errorf("creating controller runtime: %w", err)
	}

	// Register controllers.
	// ConfigController injects the parsed YAML config as a COSI resource.
	// It must go through a controller (not direct state manipulation) so that
	// the COSI runtime's internal watches detect the creation and trigger
	// downstream controllers via EventCh.
	if err := rt.RegisterController(&ConfigController{spec: cfgSpec}); err != nil {
		return fmt.Errorf("registering config controller: %w", err)
	}
	if err := rt.RegisterController(&IdentityController{}); err != nil {
		return fmt.Errorf("registering identity controller: %w", err)
	}
	if err := rt.RegisterController(&DiscoveryController{}); err != nil {
		return fmt.Errorf("registering discovery controller: %w", err)
	}
	if err := rt.RegisterController(&peerspec.PeerSpecController{}); err != nil {
		return fmt.Errorf("registering peerspec controller: %w", err)
	}
	if agentCfg.AdvertiseKubernetesNetworks && !discoveryOnly {
		if err := rt.RegisterController(&KubernetesNodeController{}); err != nil {
			return fmt.Errorf("registering k8s node controller: %w", err)
		}
	}
	if !discoveryOnly {
		if err := rt.RegisterController(&ManagerController{}); err != nil {
			return fmt.Errorf("registering manager controller: %w", err)
		}
		if err := rt.RegisterController(&networkadapter.NfTablesChainController{}); err != nil {
			return fmt.Errorf("registering nftables chain controller: %w", err)
		}
		if err := rt.RegisterController(&endpoint.EndpointController{}); err != nil {
			return fmt.Errorf("registering endpoint controller: %w", err)
		}
	}

	logger.Info("starting COSI runtime")

	// Start the COSI runtime in a goroutine.
	runtimeErrCh := make(chan error, 1)
	go func() {
		err := rt.Run(ctx)
		if err != nil {
			logger.Error("COSI runtime exited with error", zap.Error(err))
		} else {
			logger.Info("COSI runtime exited cleanly")
		}
		runtimeErrCh <- err
	}()

	if discoveryOnly {
		return waitForPeers(ctx, st, runtimeErrCh, logger)
	}

	// Full mode: run until context cancelled.
	select {
	case err := <-runtimeErrCh:
		if err != nil {
			return fmt.Errorf("controller runtime: %w", err)
		}
		return nil
	case <-ctx.Done():
		return nil
	}
}

// waitForPeers polls the COSI state for PeerSpec resources and exits when found.
func waitForPeers(ctx context.Context, st state.State, runtimeErrCh <-chan error, logger *zap.Logger) error {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case err := <-runtimeErrCh:
			if err != nil {
				return fmt.Errorf("controller runtime: %w", err)
			}
			return fmt.Errorf("controller runtime exited without finding peers")

		case <-ctx.Done():
			if ctx.Err() == context.DeadlineExceeded {
				return fmt.Errorf("timeout waiting for peers")
			}
			return nil // clean signal shutdown

		case <-ticker.C:
			list, err := safe.StateListAll[*kubespan.PeerSpec](ctx, st)
			if err != nil {
				logger.Debug("listing peers failed", zap.Error(err))
				continue
			}
			logger.Info("polling for peers", zap.Int("count", list.Len()))
			if list.Len() == 0 {
				continue
			}

			for peer := range list.All() {
				spec := peer.TypedSpec()
				logger.Info("discovered peer",
					zap.String("label", spec.Label),
					zap.String("public_key", peer.Metadata().ID()),
					zap.Stringer("address", spec.Address),
					zap.Int("endpoints", len(spec.Endpoints)),
				)
			}
			logger.Info("discovery-only mode: peers found, exiting successfully", zap.Int("count", list.Len()))
			return nil
		}
	}
}
