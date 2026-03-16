// NodeMetadataController produces COSI resources that upstream Talos
// controllers read as inputs: LocalAffiliateController, APICertSANsController,
// and APIController.
//
// Outputs:
//   - cluster.Identity       (NodeID = WireGuard public key)
//   - network.HostnameStatus (from os.Hostname)
//   - k8s.Nodename           (from agentconfig.NodeName)
//   - config.MachineType     (from agentconfig.MachineType)
//   - network.NodeAddress    x3 (routed + current + accumulative, from local interfaces)
//   - k8s.APIServerConfig    (LocalPort from cluster endpoint)
//   - k8s.Endpoint           (CP endpoints for APIController's trustd CSR flow)
//   - network.Status         (readiness gate, always ready for kubespand)
//
// Ref: talos/internal/app/machined/pkg/controllers/cluster/local_affiliate.go (consumer)
// Ref: talos/internal/app/machined/pkg/controllers/secrets/api.go (consumer)
// Ref: talos/internal/app/machined/pkg/controllers/secrets/api_cert_sans.go (consumer)
package kubespandctrl

import (
	"context"
	"fmt"
	"net/netip"
	"net/url"
	"os"
	"strconv"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/talos/pkg/machinery/config/machine"
	"github.com/siderolabs/talos/pkg/machinery/resources/cluster"
	talosconfig "github.com/siderolabs/talos/pkg/machinery/resources/config"
	"github.com/siderolabs/talos/pkg/machinery/resources/k8s"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"
	"github.com/siderolabs/talos/pkg/machinery/resources/network"
	"go.uber.org/zap"

	"github.com/agentydragon/ducktape/cluster/kubespand/agentconfig"
	"github.com/agentydragon/ducktape/cluster/kubespand/discovery"
)

// NodeMetadataController produces COSI resources consumed by the upstream
// LocalAffiliateController, derived from agentconfig and system state.
type NodeMetadataController struct{}

// Name implements controller.Controller.
func (ctrl *NodeMetadataController) Name() string {
	return "kubespand.NodeMetadataController"
}

// Inputs implements controller.Controller.
func (ctrl *NodeMetadataController) Inputs() []controller.Input {
	return []controller.Input{
		safe.Input[*kubespan.Identity](controller.InputWeak),
		safe.Input[*agentconfig.Resource](controller.InputWeak),
	}
}

// Outputs implements controller.Controller.
func (ctrl *NodeMetadataController) Outputs() []controller.Output {
	return []controller.Output{
		{Type: cluster.IdentityType, Kind: controller.OutputExclusive},
		{Type: network.HostnameStatusType, Kind: controller.OutputExclusive},
		{Type: k8s.NodenameType, Kind: controller.OutputExclusive},
		{Type: talosconfig.MachineTypeType, Kind: controller.OutputExclusive},
		{Type: network.NodeAddressType, Kind: controller.OutputExclusive},
		{Type: k8s.APIServerConfigType, Kind: controller.OutputExclusive},
		{Type: k8s.EndpointType, Kind: controller.OutputExclusive},
		{Type: network.StatusType, Kind: controller.OutputExclusive},
	}
}

// Run implements controller.Controller.
func (ctrl *NodeMetadataController) Run(ctx context.Context, r controller.Runtime, logger *zap.Logger) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		}

		ksID, err := safe.ReaderGetByID[*kubespan.Identity](ctx, r, kubespan.LocalIdentity)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}
			return fmt.Errorf("getting kubespan identity: %w", err)
		}

		acfg, err := safe.ReaderGetByID[*agentconfig.Resource](ctx, r, agentconfig.ResourceID)
		if err != nil {
			if state.IsNotFoundError(err) {
				continue
			}
			return fmt.Errorf("getting agent config: %w", err)
		}
		spec := acfg.TypedSpec()

		// 1. cluster.Identity — NodeID = WireGuard public key.
		if err := safe.WriterModify(ctx, r,
			cluster.NewIdentity(cluster.NamespaceName, cluster.LocalIdentity),
			func(res *cluster.Identity) error {
				res.TypedSpec().NodeID = ksID.TypedSpec().PublicKey
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing cluster identity: %w", err)
		}

		// 2. network.HostnameStatus — from os.Hostname().
		hostname, _ := os.Hostname()
		if err := safe.WriterModify(ctx, r,
			network.NewHostnameStatus(network.NamespaceName, network.HostnameID),
			func(res *network.HostnameStatus) error {
				res.TypedSpec().Hostname = hostname
				res.TypedSpec().Domainname = ""
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing hostname status: %w", err)
		}

		// 3. k8s.Nodename — from config.
		nodename := spec.NodeName
		if nodename == "" {
			nodename = hostname
		}
		if err := safe.WriterModify(ctx, r,
			k8s.NewNodename(k8s.NamespaceName, k8s.NodenameID),
			func(res *k8s.Nodename) error {
				res.TypedSpec().Nodename = nodename
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing nodename: %w", err)
		}

		// 4. config.MachineType — from config (non-typed resource, use raw Modify).
		machineType, _ := machine.ParseType(spec.MachineType)
		if err := r.Modify(ctx, talosconfig.NewMachineType(), func(res resource.Resource) error {
			res.(*talosconfig.MachineType).SetMachineType(machineType)
			return nil
		}); err != nil {
			return fmt.Errorf("writing machine type: %w", err)
		}

		// 5. network.NodeAddress — routed, current, and accumulative (same data for kubespand).
		// APICertSANsController reads the filtered (no-k8s) accumulative variant.
		// apid's LocalAddressProvider reads the raw NodeAddressCurrentID to determine
		// if a gRPC request target is the local node (required for worker-mode routing).
		addrs := discovery.RoutedNodeAddresses()
		prefixes := make([]netip.Prefix, 0, len(addrs))
		for _, addr := range addrs {
			prefixes = append(prefixes, netip.PrefixFrom(addr, addr.BitLen()))
		}
		routedID := network.FilteredNodeAddressID(network.NodeAddressRoutedID, k8s.NodeAddressFilterNoK8s)
		filteredCurrentID := network.FilteredNodeAddressID(network.NodeAddressCurrentID, k8s.NodeAddressFilterNoK8s)
		accumulativeID := network.FilteredNodeAddressID(network.NodeAddressAccumulativeID, k8s.NodeAddressFilterNoK8s)
		for _, id := range []resource.ID{routedID, filteredCurrentID, accumulativeID, network.NodeAddressCurrentID} {
			if err := safe.WriterModify(ctx, r,
				network.NewNodeAddress(network.NamespaceName, id),
				func(res *network.NodeAddress) error {
					res.TypedSpec().Addresses = prefixes
					return nil
				},
			); err != nil {
				return fmt.Errorf("writing node address %s: %w", id, err)
			}
		}

		// 6. k8s.APIServerConfig — LocalPort from cluster endpoint URL.
		apiPort := 6443
		if spec.ClusterEndpoint != "" {
			if u, parseErr := url.Parse(spec.ClusterEndpoint); parseErr == nil {
				if p := u.Port(); p != "" {
					if parsed, convErr := strconv.Atoi(p); convErr == nil {
						apiPort = parsed
					}
				}
			}
		}
		if err := safe.WriterModify(ctx, r,
			k8s.NewAPIServerConfig(),
			func(res *k8s.APIServerConfig) error {
				res.TypedSpec().LocalPort = apiPort
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing api server config: %w", err)
		}

		// 7. k8s.Endpoint — CP endpoints for APIController's trustd CSR flow.
		// The APIController (worker mode) reads k8s.Endpoint resources to find
		// trustd endpoints for CSR submission. Derive from cluster.endpoint URL.
		if spec.ClusterEndpoint != "" {
			if u, parseErr := url.Parse(spec.ClusterEndpoint); parseErr == nil {
				host := u.Hostname()
				if addr, addrErr := netip.ParseAddr(host); addrErr == nil {
					if err := safe.WriterModify(ctx, r,
						k8s.NewEndpoint(k8s.ControlPlaneNamespaceName, k8s.ControlPlaneKubernetesEndpointsID),
						func(res *k8s.Endpoint) error {
							res.TypedSpec().Addresses = []netip.Addr{addr}
							return nil
						},
					); err != nil {
						return fmt.Errorf("writing CP endpoint: %w", err)
					}
				}
			}
		}

		// 8. network.Status — readiness gate for APIController.
		// Set always-ready because NodeMetadataController doesn't dynamically track
		// host network state (it only re-runs when kubespan.Identity or agentconfig
		// change). On a laptop that loses connectivity, addresses go stale anyway.
		// TODO: add a network watcher that triggers re-reconciliation on address changes.
		if err := safe.WriterModify(ctx, r,
			network.NewStatus(network.NamespaceName, network.StatusID),
			func(res *network.Status) error {
				res.TypedSpec().AddressReady = true
				res.TypedSpec().ConnectivityReady = true
				res.TypedSpec().HostnameReady = true
				res.TypedSpec().EtcFilesReady = true
				return nil
			},
		); err != nil {
			return fmt.Errorf("writing network status: %w", err)
		}

		logger.Debug("node metadata reconciled",
			zap.String("node_id", ksID.TypedSpec().PublicKey),
			zap.String("hostname", hostname),
			zap.Int("addresses", len(prefixes)),
		)
		r.ResetRestartBackoff()
	}
}
