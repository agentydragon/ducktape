# Node Recreate Changes podCIDR; Surviving Pods Keep Unroutable IPs

**Date**: 2026-03-19
**Status**: Resolved

Recreating a node assigns it a fresh podCIDR, and Cilium after its restart routes
only the new range. DaemonSet pods that survive the recreate keep IPs from the old
range and are silently unroutable — they look Ready-ish but cannot reach ClusterIP
services ("no route to host"). Fix: delete those pods so they reschedule with IPs
from the new range.

Context: seen on the since-decommissioned legacy VPS control plane
(`talos-vps-cp-1`), where one such stale `longhorn-manager` pod cascaded into 84
blocked Flux kustomizations.
