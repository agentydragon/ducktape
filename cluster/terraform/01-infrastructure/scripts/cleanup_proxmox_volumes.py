#!/usr/bin/env python3
"""
Cleanup Proxmox volumes for retained PVs during terraform destroy.

Strategy:
1. Query Kubernetes for Proxmox CSI PV volume handles
2. Extract Proxmox volume IDs from handles
3. Delete volumes via SSH to Proxmox host

IMPORTANT: This script MUST run while cluster API is accessible.
It will fail if it cannot query Kubernetes to avoid deleting wrong volumes.
"""

import json
import subprocess
import sys


def get_volumes_from_kubernetes(kubeconfig_path: str) -> list[str]:
    """Get Proxmox volume IDs from Kubernetes PVs."""
    try:
        result = subprocess.run(
            ["kubectl", f"--kubeconfig={kubeconfig_path}", "get", "pv", "-o", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []

        pvs = json.loads(result.stdout)
        volumes = []

        for item in pvs.get("items", []):
            spec = item.get("spec", {})

            # Filter for Proxmox CSI with retain policy
            if (
                spec.get("storageClassName") == "proxmox-csi-retain"
                and spec.get("csi", {}).get("driver") == "csi.proxmox.sinextra.dev"
            ):
                # Extract volume handle: cluster/atlas/local/9999/vm-9999-pvc-XXX.raw
                # Convert to: local:9999/vm-9999-pvc-XXX.raw
                volume_handle = spec.get("csi", {}).get("volumeHandle", "")
                if volume_handle:
                    # Remove "cluster/<node>/" prefix
                    parts = volume_handle.split("/")
                    if len(parts) >= 3:
                        # Rejoin storage/vmid/volume with : separator after storage
                        volume_id = f"{parts[2]}:{'/'.join(parts[3:])}"
                        volumes.append(volume_id)

        return volumes

    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        return []


def delete_volume(proxmox_host: str, volume_id: str) -> bool:
    """Delete a volume from Proxmox storage."""
    try:
        result = subprocess.run(
            ["ssh", proxmox_host, f"pvesm free {volume_id}"], check=False, capture_output=True, timeout=30
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


def main():
    if len(sys.argv) < 3:
        print("Usage: cleanup_proxmox_volumes.py <kubeconfig_path> <proxmox_ssh_host>")
        print("  proxmox_ssh_host: Tailscale hostname (e.g., 'atlas'), NOT the FQDN")
        sys.exit(1)
    kubeconfig_path = sys.argv[1]
    proxmox_ssh_host = sys.argv[2]
    proxmox_ssh_target = f"root@{proxmox_ssh_host}"

    print("🧹 Cleaning up Proxmox volumes from retained PVs...")

    # Query Kubernetes - MUST be accessible at this stage
    volumes = get_volumes_from_kubernetes(kubeconfig_path)

    if not volumes:
        print("❌ ERROR: Cluster API unavailable - cannot safely identify volumes")
        print(f"Manual cleanup: ssh {proxmox_ssh_target} 'pvesm list local | grep pvc-'")
        return 1

    print(f"📋 Found {len(volumes)} volumes to delete:")
    for vol in volumes:
        print(f"  - {vol}")

    # Delete each volume
    cleaned = 0
    failed = 0

    for vol in volumes:
        print(f"🗑️  Deleting: {vol}")
        if delete_volume(proxmox_ssh_target, vol):
            cleaned += 1
        else:
            print(f"⚠️  Failed to delete {vol} (may not exist)")
            failed += 1

    print(f"✅ Cleanup complete: {cleaned} deleted, {failed} failed/not found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
