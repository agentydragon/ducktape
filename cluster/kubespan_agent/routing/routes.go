// Kubespand-only: InstallRoutes adds default routes to the kubespan routing table.
// Talos manages routes via COSI network.RouteSpec resources instead.
package kubespan

import (
	"fmt"
	"net"

	"github.com/siderolabs/talos/pkg/machinery/constants"
	"github.com/vishvananda/netlink"
)

// InstallRoutes adds default routes in table 180 pointing to the kubespan interface.
// Ref: talos/internal/app/machined/pkg/controllers/kubespan/manager.go (RouteSpec)
func InstallRoutes(mtu int) error {
	link, err := netlink.LinkByName(constants.KubeSpanLinkName)
	if err != nil {
		return fmt.Errorf("finding %s for routes: %w", constants.KubeSpanLinkName, err)
	}

	v4Route := &netlink.Route{
		LinkIndex: link.Attrs().Index,
		Table:     constants.KubeSpanDefaultRoutingTable,
		Dst:       &net.IPNet{IP: net.IPv4zero, Mask: net.CIDRMask(0, 32)},
		MTU:       mtu,
	}
	if err := netlink.RouteReplace(v4Route); err != nil {
		return fmt.Errorf("adding IPv4 default route to table %d: %w", constants.KubeSpanDefaultRoutingTable, err)
	}

	v6Route := &netlink.Route{
		LinkIndex: link.Attrs().Index,
		Table:     constants.KubeSpanDefaultRoutingTable,
		Dst:       &net.IPNet{IP: net.IPv6zero, Mask: net.CIDRMask(0, 128)},
		MTU:       mtu,
	}
	if err := netlink.RouteReplace(v6Route); err != nil {
		return fmt.Errorf("adding IPv6 default route to table %d: %w", constants.KubeSpanDefaultRoutingTable, err)
	}

	return nil
}
