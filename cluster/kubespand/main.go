package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	controllerruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	v1alpha1runtime "github.com/siderolabs/talos/internal/app/machined/pkg/runtime"
	"github.com/siderolabs/talos/pkg/machinery/config/config"
	"github.com/siderolabs/talos/pkg/machinery/config/types/v1alpha1"
	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/siderolabs/talos/pkg/machinery/version"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/cluster/kubespand/agentconfig"
	"github.com/agentydragon/ducktape/cluster/kubespand/api"
	clusterctrl "github.com/agentydragon/ducktape/cluster/kubespand/controllers/cluster"
	k8sctrl "github.com/agentydragon/ducktape/cluster/kubespand/controllers/k8s"
	kubespanctrl "github.com/agentydragon/ducktape/cluster/kubespand/controllers/kubespan"
	kubespandctrl "github.com/agentydragon/ducktape/cluster/kubespand/controllers/kubespand"
	networkctrl "github.com/agentydragon/ducktape/cluster/kubespand/controllers/network"
	taloscontrollerscluster "github.com/siderolabs/talos/internal/app/machined/pkg/controllers/cluster"
	taloscontrollersk8s "github.com/siderolabs/talos/internal/app/machined/pkg/controllers/k8s"
	taloscontrollerskubespan "github.com/siderolabs/talos/internal/app/machined/pkg/controllers/kubespan"
	taloscontrollersnetwork "github.com/siderolabs/talos/internal/app/machined/pkg/controllers/network"
	taloscontrollersruntime "github.com/siderolabs/talos/internal/app/machined/pkg/controllers/runtime"
	taloscontrollerssecrets "github.com/siderolabs/talos/internal/app/machined/pkg/controllers/secrets"
)

func init() {
	// Override Talos version vars so LocalAffiliateController produces
	// a kubespand-specific OperatingSystem string instead of Talos's.
	version.Name = "kubespand"
	version.Tag = "0.1.0"
}

func main() {
	configPath := flag.String("config", "/etc/kubespan/agent.yaml", "path to config file")
	debug := flag.Bool("debug", false, "enable debug logging")
	flag.Parse()

	cfg := zap.NewProductionConfig()
	if *debug {
		cfg.Level = zap.NewAtomicLevelAt(zap.DebugLevel)
	}
	logger, err := cfg.Build()
	if err != nil {
		fmt.Fprintf(os.Stderr, "logger: %v\n", err)
		os.Exit(1)
	}
	defer logger.Sync() //nolint:errcheck

	if err := run(*configPath, logger); err != nil {
		logger.Fatal("kubespand exited with error", zap.Error(err))
	}
}

func run(configPath string, logger *zap.Logger) error {
	// Load agent config from YAML.
	cfg, err := agentconfig.Load(configPath)
	if err != nil {
		return fmt.Errorf("config: %w", err)
	}
	logger.Info("loaded config",
		zap.String("cluster_id", cfg.Cluster.ID),
		zap.String("discovery_endpoint", cfg.Discovery.Endpoint),
		zap.Uint32("mtu", cfg.Kubespan.MTU),
	)

	// Convert to upstream resource specs for COSI injection.
	kubespanSpec := cfg.ToConfigSpec()
	clusterSpec, err := cfg.ToClusterConfigSpec()
	if err != nil {
		return fmt.Errorf("cluster config: %w", err)
	}
	agentSpec := agentconfig.SpecFromAgentConfig(cfg)

	// Create COSI in-memory state.
	st := state.WrapCore(namespaced.NewState(inmem.Build))

	// Set up context with signal handling.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

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

	// Build the network device shim for RouteConfigController if routes are configured.
	var networkDevice config.Device
	if len(cfg.Network.Routes) > 0 {
		if cfg.Network.Interface == "" {
			return fmt.Errorf("network.interface is required when network.routes are configured")
		}
		networkDevice = &v1alpha1.Device{
			DeviceInterface: cfg.Network.Interface,
			DeviceRoutes:    cfg.Network.Routes,
		}
	}

	// Register controllers.
	if err := rt.RegisterController(&kubespandctrl.ConfigController{
		KubespanSpec:           kubespanSpec,
		ClusterSpec:            clusterSpec,
		AgentSpec:              agentSpec,
		KubernetesServiceCIDRs: cfg.Kubernetes.ServiceCIDRs,
		NetworkDevice:          networkDevice,
	}); err != nil {
		return fmt.Errorf("registering config controller: %w", err)
	}
	if err := rt.RegisterController(&kubespanctrl.IdentityController{}); err != nil {
		return fmt.Errorf("registering identity controller: %w", err)
	}
	if err := rt.RegisterController(&kubespandctrl.NodeMetadataController{}); err != nil {
		return fmt.Errorf("registering node metadata controller: %w", err)
	}
	if err := rt.RegisterController(&taloscontrollerscluster.LocalAffiliateController{}); err != nil {
		return fmt.Errorf("registering local affiliate controller: %w", err)
	}
	if err := rt.RegisterController(&clusterctrl.DiscoveryController{}); err != nil {
		return fmt.Errorf("registering discovery controller: %w", err)
	}
	if err := rt.RegisterController(&taloscontrollerskubespan.PeerSpecController{}); err != nil {
		return fmt.Errorf("registering peerspec controller: %w", err)
	}
	if cfg.Kubernetes.AdvertiseNetworks {
		if err := rt.RegisterController(&clusterctrl.KubernetesNodeController{}); err != nil {
			return fmt.Errorf("registering k8s node controller: %w", err)
		}
	}
	if cfg.KubePrism.Enabled {
		if err := rt.RegisterController(&k8sctrl.KubePrismConfigController{}); err != nil {
			return fmt.Errorf("registering kubeprism config controller: %w", err)
		}
		if err := rt.RegisterController(&taloscontrollersk8s.KubePrismController{}); err != nil {
			return fmt.Errorf("registering kubeprism controller: %w", err)
		}
	}
	if cfg.Api.CACrt != "" {
		if err := rt.RegisterController(&kubespandctrl.OSRootController{}); err != nil {
			return fmt.Errorf("registering OS root controller: %w", err)
		}
		if err := rt.RegisterController(&taloscontrollerssecrets.APICertSANsController{}); err != nil {
			return fmt.Errorf("registering API cert SANs controller: %w", err)
		}
		if err := rt.RegisterController(&taloscontrollerssecrets.APIController{}); err != nil {
			return fmt.Errorf("registering API controller: %w", err)
		}
	}
	if err := rt.RegisterController(&kubespanctrl.ManagerController{}); err != nil {
		return fmt.Errorf("registering manager controller: %w", err)
	}
	if err := rt.RegisterController(&networkctrl.WireguardLinkController{}); err != nil {
		return fmt.Errorf("registering wireguard link controller: %w", err)
	}
	if err := rt.RegisterController(&taloscontrollersnetwork.NfTablesChainController{}); err != nil {
		return fmt.Errorf("registering nftables chain controller: %w", err)
	}
	if err := rt.RegisterController(&taloscontrollersnetwork.AddressSpecController{}); err != nil {
		return fmt.Errorf("registering address spec controller: %w", err)
	}
	if err := rt.RegisterController(&taloscontrollersnetwork.RouteSpecController{}); err != nil {
		return fmt.Errorf("registering route spec controller: %w", err)
	}
	if err := rt.RegisterController(&taloscontrollersnetwork.RouteConfigController{}); err != nil {
		return fmt.Errorf("registering route config controller: %w", err)
	}
	if err := rt.RegisterController(taloscontrollersnetwork.NewRouteMergeController()); err != nil {
		return fmt.Errorf("registering route merge controller: %w", err)
	}
	if err := rt.RegisterController(&taloscontrollerskubespan.EndpointController{}); err != nil {
		return fmt.Errorf("registering endpoint controller: %w", err)
	}

	// Network status: upstream controllers for live address/link monitoring.
	// Replaces static address snapshot in NodeMetadataController.
	if err := rt.RegisterController(&taloscontrollersnetwork.AddressStatusController{}); err != nil {
		return fmt.Errorf("registering address status controller: %w", err)
	}
	if err := rt.RegisterController(&taloscontrollersnetwork.LinkStatusController{}); err != nil {
		return fmt.Errorf("registering link status controller: %w", err)
	}
	if err := rt.RegisterController(&taloscontrollersnetwork.NodeAddressController{}); err != nil {
		return fmt.Errorf("registering node address controller: %w", err)
	}

	// Kernel management: declarative module loading and sysctl application.
	// Replaces imperative os.WriteFile calls in WireguardLinkController.
	if err := rt.RegisterController(&taloscontrollersruntime.KernelModuleSpecController{
		V1Alpha1Mode: v1alpha1runtime.ModeMetal,
	}); err != nil {
		return fmt.Errorf("registering kernel module spec controller: %w", err)
	}
	if err := rt.RegisterController(&taloscontrollersruntime.KernelParamSpecController{}); err != nil {
		return fmt.Errorf("registering kernel param spec controller: %w", err)
	}
	// Note: KernelParamDefaultsController is NOT registered. It produces Talos
	// KSPP hardening defaults (yama/ptrace_scope, unprivileged_userfaultfd, etc.)
	// which don't exist on non-Talos kernels (Alpine, NixOS) and would crash-loop
	// KernelParamSpecController. kubespand only needs the specific sysctls
	// produced by ConfigController (rp_filter, src_valid_mark).

	logger.Info("starting COSI runtime")

	// Start the API server (COSI state on Unix socket, optionally TCP).
	apiServer := api.NewServer(st, constants.MachineSocketPath, cfg.Api.ListenTCP, logger)
	apiErrCh := make(chan error, 1)
	go func() {
		apiErrCh <- apiServer.Run(ctx)
	}()

	// Start apid subprocess management if configured.
	// Follows Talos pattern: wait for secrets.API (APIReadyCondition) then start apid.
	apidErrCh := make(chan error, 1)
	if cfg.Api.ApidPath != "" {
		go func() {
			apidErrCh <- runApid(ctx, st, cfg.Api.ApidPath, logger)
		}()
	}

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

	// Run until context cancelled or a subsystem fails.
	select {
	case err := <-runtimeErrCh:
		if err != nil {
			return fmt.Errorf("controller runtime: %w", err)
		}
		return nil
	case err := <-apiErrCh:
		if err != nil {
			return fmt.Errorf("API server: %w", err)
		}
		return nil
	case err := <-apidErrCh:
		if err != nil {
			return fmt.Errorf("apid: %w", err)
		}
		return nil
	case <-ctx.Done():
		return nil
	}
}
