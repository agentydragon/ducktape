// Package vmconst defines constants shared between the QEMU test framework
// (qemu_tests package) and the VM init processes (initlib and friends).
// Both sides import this package as the single source of truth.
package vmconst

// MgmtMAC is the well-known MAC address assigned to the management NIC.
// BootVM on the test host always uses this MAC, allowing the VM init to
// find the mgmt NIC regardless of how many mesh NICs precede it.
const MgmtMAC = "52:54:00:aa:00:01"

// MgmtIP is the IP address assigned to the management NIC (QEMU user-mode default).
const MgmtIP = "10.0.2.15"
