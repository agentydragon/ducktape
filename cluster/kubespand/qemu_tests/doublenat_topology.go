// Shared network topology constants for double-NAT test scenarios.
// Used by the parameterized doublenat test (both kubespand and Talos workers).
package qemu_tests

// Double-NAT topology:
//
//   [NAT1 192.168.60.2] --[LAN-A 192.168.60.0/24]-- [Router-A] --+
//                                                                   |
//                                              [Internet 192.168.50.0/24]
//                                                                   |
//   [NAT2 192.168.70.2] --[LAN-B 192.168.70.0/24]-- [Router-B] --+
//                                                                   |
//                                               [VPS 192.168.50.2]  |
//                                          [Discovery 192.168.50.254]

// Internet subnet (shared L2 segment for VPS, routers, discovery).
const (
	DoubleNATDiscoveryIP   = "192.168.50.254"
	DoubleNATDiscoveryAddr = DoubleNATDiscoveryIP + ":3000"
	DoubleNATDiscoveryCIDR = DoubleNATDiscoveryIP + "/24"

	DoubleNATVPSIP = "192.168.50.2"

	DoubleNATRouterAInternetCIDR = "192.168.50.1/24"
	DoubleNATRouterBInternetCIDR = "192.168.50.3/24"
)

// LAN-A subnet (behind Router-A).
const (
	DoubleNATRouterALanCIDR = "192.168.60.1/24"
	DoubleNATNAT1IP         = "192.168.60.2"
	DoubleNATNAT1Gateway    = "192.168.60.1"
)

// LAN-B subnet (behind Router-B).
const (
	DoubleNATRouterBLanCIDR = "192.168.70.1/24"
	DoubleNATNAT2IP         = "192.168.70.2"
	DoubleNATNAT2Gateway    = "192.168.70.1"
)

// MAC addresses for double-NAT infrastructure VMs.
const (
	DoubleNATDiscoveryMAC       = DiscoveryMAC
	DoubleNATVPSMAC             = NodeAMAC
	DoubleNATNAT1MAC            = "52:54:00:d0:00:01"
	DoubleNATNAT2MAC            = "52:54:00:e0:00:01"
	DoubleNATRouterAInternetMAC = "52:54:00:c1:00:01"
	DoubleNATRouterALanMAC      = "52:54:00:c1:00:02"
	DoubleNATRouterBInternetMAC = "52:54:00:c2:00:01"
	DoubleNATRouterBLanMAC      = "52:54:00:c2:00:02"
)
