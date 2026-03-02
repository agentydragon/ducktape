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
	"go.uber.org/zap"
)

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
	defer logger.Sync() //nolint:errcheck

	if err := run(*configPath, *discoveryOnly, *discoveryTimeout, logger); err != nil {
		logger.Fatal("kubespand exited with error", zap.Error(err))
	}
}

func run(configPath string, discoveryOnly bool, discoveryTimeout time.Duration, logger *zap.Logger) error {
	// Load config from YAML.
	cfgSpec, err := LoadConfig(configPath)
	if err != nil {
		return fmt.Errorf("config: %w", err)
	}
	logger.Info("loaded config",
		zap.String("cluster_id", cfgSpec.ClusterID),
		zap.String("discovery_endpoint", cfgSpec.DiscoveryEndpoint),
		zap.Int("listen_port", cfgSpec.ListenPort),
		zap.Int("mtu", cfgSpec.MTU),
		zap.Bool("discovery_only", discoveryOnly),
	)

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

	// Inject config as a COSI resource.
	if err := st.Create(ctx, NewConfig(KubespanNamespace, ConfigID)); err != nil {
		return fmt.Errorf("creating config resource: %w", err)
	}
	if err := safe.StateModify(ctx, st, NewConfig(KubespanNamespace, ConfigID), func(res *Config) error {
		*res.TypedSpec() = *cfgSpec
		return nil
	}); err != nil {
		return fmt.Errorf("populating config resource: %w", err)
	}

	// Create COSI controller runtime.
	rt, err := controllerruntime.NewRuntime(st, logger)
	if err != nil {
		return fmt.Errorf("creating controller runtime: %w", err)
	}

	// Register controllers.
	if err := rt.RegisterController(&IdentityController{}); err != nil {
		return fmt.Errorf("registering identity controller: %w", err)
	}
	if err := rt.RegisterController(&DiscoveryController{}); err != nil {
		return fmt.Errorf("registering discovery controller: %w", err)
	}
	if !discoveryOnly {
		if err := rt.RegisterController(&ManagerController{}); err != nil {
			return fmt.Errorf("registering manager controller: %w", err)
		}
	}

	// Start the COSI runtime in a goroutine.
	runtimeErrCh := make(chan error, 1)
	go func() {
		runtimeErrCh <- rt.Run(ctx)
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
			return fmt.Errorf("timeout waiting for peers")

		case <-ticker.C:
			list, err := safe.StateListAll[*PeerSpec](ctx, st)
			if err != nil {
				continue
			}
			if list.Len() == 0 {
				continue
			}

			for peer := range list.All() {
				spec := peer.TypedSpec()
				logger.Info("discovered peer",
					zap.String("label", spec.Label),
					zap.String("public_key", spec.PublicKey),
					zap.String("address", spec.Address.String()),
					zap.Int("endpoints", len(spec.Endpoints)),
				)
			}
			logger.Info("discovery-only mode: peers found, exiting successfully", zap.Int("count", list.Len()))
			return nil
		}
	}
}
