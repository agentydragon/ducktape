// ConfigController injects the agent's parsed configuration into the COSI state
// as domain-specific resources consumed by upstream Talos controllers:
//
//   - kubespan.Config — WireGuard/routing settings (upstream type)
//   - cluster.Config  — discovery service settings (upstream type)
//   - agentconfig.Resource — kubespand-specific fields (custom type)
//   - runtime.KernelModuleSpec — kernel modules to load (e.g. "wireguard")
//   - runtime.KernelParamSpec — sysctls to apply (rp_filter, src_valid_mark)
//   - network.NodeAddressFilter — K8s subnet exclusion for NodeAddressController
//   - network.NodeAddressSortAlgorithm — address sorting config
//
// This mirrors Talos's pattern where MachineConfig is decomposed into
// domain-specific config resources by dedicated controllers.
//
// Ref: talos/internal/app/machined/pkg/controllers/config/ (upstream decomposition)
// Ref: talos/internal/app/machined/pkg/controllers/runtime/kernel_module_config.go
// Ref: talos/internal/app/machined/pkg/controllers/runtime/kernel_param_config.go
// Ref: talos/internal/app/machined/pkg/controllers/k8s/address_filter.go
// Ref: talos/internal/app/machined/pkg/controllers/network/node_address_sort_algorithm.go
package kubespandctrl

import (
	"context"
	"fmt"
	"net/netip"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/siderolabs/talos/pkg/machinery/nethelpers"
	"github.com/siderolabs/talos/pkg/machinery/resources/cluster"
	talosconfig "github.com/siderolabs/talos/pkg/machinery/resources/config"
	"github.com/siderolabs/talos/pkg/machinery/resources/k8s"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"github.com/siderolabs/talos/pkg/machinery/resources/network"
	"github.com/siderolabs/talos/pkg/machinery/resources/runtime"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/cluster/kubespand/agentconfig"
)

// ConfigController injects parsed YAML config into COSI state as domain-specific
// resources for upstream Talos controllers.
type ConfigController struct {
	KubespanSpec kubespan.ConfigSpec
	ClusterSpec  cluster.ConfigSpec
	AgentSpec    agentconfig.Spec

	// KubernetesServiceCIDRs are K8s service network ranges to exclude from
	// node addresses (injected into NodeAddressFilter).
	KubernetesServiceCIDRs []netip.Prefix
}

// Name implements controller.Controller.
func (ctrl *ConfigController) Name() string {
	return "kubespand.ConfigController"
}

// Inputs implements controller.Controller.
func (ctrl *ConfigController) Inputs() []controller.Input {
	return nil
}

// Outputs implements controller.Controller.
func (ctrl *ConfigController) Outputs() []controller.Output {
	return []controller.Output{
		{Type: kubespan.ConfigType, Kind: controller.OutputExclusive},
		{Type: cluster.ConfigType, Kind: controller.OutputExclusive},
		{Type: agentconfig.ResourceType, Kind: controller.OutputExclusive},
		{Type: runtime.KernelModuleSpecType, Kind: controller.OutputShared},
		{Type: runtime.KernelParamSpecType, Kind: controller.OutputShared},
		{Type: network.NodeAddressFilterType, Kind: controller.OutputExclusive},
		{Type: network.NodeAddressSortAlgorithmType, Kind: controller.OutputExclusive},
	}
}

// Run implements controller.Controller.
func (ctrl *ConfigController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		}

		if err := safe.WriterModify(ctx, r,
			kubespan.NewConfig(talosconfig.NamespaceName, kubespan.ConfigID),
			func(res *kubespan.Config) error {
				*res.TypedSpec() = ctrl.KubespanSpec
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing kubespan config: %w", err)
		}

		if err := safe.WriterModify(ctx, r,
			cluster.NewConfig(talosconfig.NamespaceName, cluster.ConfigID),
			func(res *cluster.Config) error {
				*res.TypedSpec() = ctrl.ClusterSpec
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing cluster config: %w", err)
		}

		if err := safe.WriterModify(ctx, r,
			agentconfig.NewResource(),
			func(res *agentconfig.Resource) error {
				*res.TypedSpec() = ctrl.AgentSpec
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing agent config: %w", err)
		}

		// Kernel module: wireguard.
		// Replaces manual modprobe; upstream KernelModuleSpecController loads via kmod.
		if err := safe.WriterModify(ctx, r,
			runtime.NewKernelModuleSpec(runtime.NamespaceName, "wireguard"),
			func(res *runtime.KernelModuleSpec) error {
				res.TypedSpec().Name = "wireguard"
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing kernel module spec: %w", err)
		}

		// Kernel params: sysctls needed for KubeSpan routing.
		// Replaces imperative os.WriteFile in WireguardLinkController.
		// Upstream KernelParamSpecController reads these and writes /proc/sys/*.
		// Keys use proc.sys. prefix (matching upstream Talos convention).
		sysctls := map[string]string{
			// rp_filter: effective = max(conf/all, conf/iface). Disable both.
			// Non-Talos hosts (NixOS, Ubuntu) set rp_filter=1 via sysctl.d;
			// without this, decrypted packets on kubespan get dropped.
			"proc.sys.net.ipv4.conf.all.rp_filter":      "0",
			"proc.sys.net.ipv4.conf.kubespan.rp_filter": "0",
			// src_valid_mark: include fwmark in reverse path validation.
			// WireGuard-recommended for fwmark-based policy routing.
			"proc.sys.net.ipv4.conf.all.src_valid_mark": "1",
		}
		for key, value := range sysctls {
			if err := safe.WriterModify(ctx, r,
				runtime.NewKernelParamSpec(runtime.NamespaceName, key),
				func(res *runtime.KernelParamSpec) error {
					res.TypedSpec().Value = value
					return nil
				},
			); err != nil {
				return fmt.Errorf("writing kernel param spec %s: %w", key, err)
			}
		}

		// Node address filter: exclude K8s subnets from routed addresses.
		// Replaces upstream k8s.AddressFilterController which reads MachineConfig.
		if err := safe.WriterModify(ctx, r,
			network.NewNodeAddressFilter(network.NamespaceName, k8s.NodeAddressFilterNoK8s),
			func(res *network.NodeAddressFilter) error {
				res.TypedSpec().ExcludeSubnets = ctrl.KubernetesServiceCIDRs
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing node address filter: %w", err)
		}

		// Node address sort algorithm: default V1 (matches upstream default).
		if err := safe.WriterModify(ctx, r,
			network.NewNodeAddressSortAlgorithm(network.NamespaceName, network.NodeAddressSortAlgorithmID),
			func(res *network.NodeAddressSortAlgorithm) error {
				res.TypedSpec().Algorithm = nethelpers.AddressSortAlgorithmV1
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing node address sort algorithm: %w", err)
		}

		logger.Info("config resources injected via COSI controller")
		r.ResetRestartBackoff()
	}
}
