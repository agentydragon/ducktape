"""Attribute block-device I/O on a Talos node to cgroups (etcd, containerd, pods).

kubelet's embedded cAdvisor only walks `/kubepods`, so etcd -- which Talos runs in
its own `podruntime/etcd` cgroup -- is invisible to it. Reading cgroup v2 `io.stat`
directly attributes every writer, and splits per block device, which is what
separates "on the etcd disk" from "on the data disk".

Counters are cumulative since boot, so this samples twice and reports rates.

    bazel run //cluster/scripts:talos_cgroup_io -- --node 10.42.0.13
    bazel run //cluster/scripts:talos_cgroup_io -- --node 10.42.0.13 --json

Caveats that matter when reading the output, learned the hard way:

- **Rates are extremely bursty.** A single cgroup has been observed at 151 KiB/s in
  one window and 99 MiB/s in the next. Take several windows (`--repeat`) before
  concluding anything about steady state.
- **Values are not cleanly additive.** cgroup v2 charges writeback to the cgroup
  that dirtied the page, at writeback time, so a parent's own charge can exceed or
  undershoot the sum of its children in any given window.
- **Device enumeration is not stable across hosts.** Which NVMe holds `/var` differs
  between otherwise identical machines, so the disk roles are resolved from
  `/proc/mounts` per node rather than assumed.
"""

import argparse
import json
import subprocess
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Disk:
    device: str
    mountpoints: tuple[str, ...]

    @property
    def role(self) -> str:
        if any(m == "/var" for m in self.mountpoints):
            return "etcd/install"
        if any(m.startswith("/var/mnt/") for m in self.mountpoints):
            return "data"
        return "other"


@dataclass
class Sample:
    wbytes: int
    wios: int
    rbytes: int
    rios: int


@dataclass
class Node:
    address: str
    disks: dict[str, Disk] = field(default_factory=dict)


def talos(address: str, args: list[str], timeout: int = 60) -> str:
    # -e is required: without an explicit endpoint the apid call fails
    # "PermissionDenied: no request forwarding".
    result = subprocess.run(
        ["talosctl", "-e", address, "-n", address, *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    return result.stdout if result.returncode == 0 else ""


def read_disks(address: str) -> dict[str, Disk]:
    """major:minor -> Disk, resolving each device's role from its mountpoints."""
    partitions: dict[str, str] = {}
    for line in talos(address, ["read", "/proc/partitions"]).splitlines()[2:]:
        fields = line.split()
        if len(fields) == 4:
            partitions[fields[3]] = f"{fields[0]}:{fields[1]}"

    mounts: dict[str, list[str]] = {}
    for line in talos(address, ["read", "/proc/mounts"]).splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].startswith("/dev/"):
            mounts.setdefault(fields[0].removeprefix("/dev/"), []).append(fields[1])

    disks: dict[str, Disk] = {}
    for name, devno in partitions.items():
        # A partition's mountpoint describes the whole disk's role: /var living on
        # nvme1n1p5 makes nvme1n1 the etcd disk, and io.stat keys on the disk.
        points = [m for dev, ms in mounts.items() if dev.startswith(name) for m in ms]
        disks[devno] = Disk(device=name, mountpoints=tuple(sorted(set(points))))
    return disks


def cgroup_paths(address: str) -> list[str]:
    paths = ["podruntime/etcd", "podruntime/kubelet", "podruntime/runtime", "system", "init", "kubepods"]

    def children(parent: str) -> list[str]:
        found = []
        for line in talos(address, ["ls", f"/sys/fs/cgroup/{parent}"]).splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2 and fields[1].startswith("pod"):
                found.append(f"{parent}/{fields[1]}")
        return found

    # Burstable and BestEffort pods nest under a QoS directory; Guaranteed pods sit
    # directly under kubepods. Missing that last case hides the heaviest writers.
    for qos in ("kubepods/burstable", "kubepods/besteffort"):
        paths.extend(children(qos))
    paths.extend(children("kubepods"))
    return paths


def read_io(address: str, path: str) -> dict[str, Sample]:
    out: dict[str, Sample] = {}
    for line in talos(address, ["read", f"/sys/fs/cgroup/{path}/io.stat"]).splitlines():
        fields = line.split()
        if not fields:
            continue
        values = dict(pair.split("=") for pair in fields[1:] if "=" in pair)
        out[fields[0]] = Sample(
            wbytes=int(values["wbytes"]),
            wios=int(values["wios"]),
            rbytes=int(values["rbytes"]),
            rios=int(values["rios"]),
        )
    return out


def pod_names(address: str) -> dict[str, str]:
    """Container-id prefix -> "namespace/pod", for labelling pod cgroups."""
    names = {}
    for line in talos(address, ["containers", "-k"]).splitlines()[1:]:
        if ":" not in line:
            continue
        ident = line.split()[2] if len(line.split()) > 2 else ""
        parts = ident.split(":")
        if len(parts) == 3:
            names[parts[2]] = parts[0].lstrip("└─ ")
    return names


def label(address: str, path: str, names: dict[str, str]) -> str:
    if not path.rsplit("/", 1)[-1].startswith("pod"):
        return path
    for line in talos(address, ["ls", f"/sys/fs/cgroup/{path}"]).splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and len(fields[1]) == 64 and (name := names.get(fields[1][:12])):
            return f"{path.rsplit('/', 1)[0]}/ {name}"
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--node", action="append", required=True, help="node address (repeatable)")
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between samples")
    parser.add_argument("--repeat", type=int, default=1, help="number of sampling windows")
    parser.add_argument("--min-kib", type=float, default=1.0, help="hide cgroups below this write rate")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()

    nodes = [Node(address=a) for a in args.node]
    for node in nodes:
        node.disks = read_disks(node.address)

    paths = {node.address: cgroup_paths(node.address) for node in nodes}
    names = {node.address: pod_names(node.address) for node in nodes}
    records = []

    for node in nodes:
        previous = {p: read_io(node.address, p) for p in paths[node.address]}
        previous_at = time.time()
        for _ in range(args.repeat):
            time.sleep(args.interval)
            current = {p: read_io(node.address, p) for p in paths[node.address]}
            elapsed = time.time() - previous_at
            for path, devices in current.items():
                for devno, now in devices.items():
                    before = previous.get(path, {}).get(devno)
                    disk = node.disks.get(devno)
                    if before is None or disk is None:
                        continue
                    records.append(
                        {
                            "node": node.address,
                            "cgroup": label(node.address, path, names[node.address]),
                            "device": disk.device,
                            "disk_role": disk.role,
                            "write_kib_s": (now.wbytes - before.wbytes) / elapsed / 1024,
                            "write_ios_s": (now.wios - before.wios) / elapsed,
                            "read_kib_s": (now.rbytes - before.rbytes) / elapsed / 1024,
                            "window_s": round(elapsed, 1),
                        }
                    )
            previous, previous_at = current, time.time()

    if args.json:
        print(json.dumps(records, indent=2))
        return

    for node in nodes:
        print(f"\n=== {node.address}")
        rows = [r for r in records if r["node"] == node.address and r["write_kib_s"] >= args.min_kib]
        rows.sort(key=lambda r: r["write_kib_s"], reverse=True)
        print(f"{'write KiB/s':>12} {'w ops/s':>9} {'device':>10} {'role':>12}  cgroup")
        for row in rows:
            print(
                f"{row['write_kib_s']:12.1f} {row['write_ios_s']:9.1f} "
                f"{row['device']:>10} {row['disk_role']:>12}  {row['cgroup']}"
            )


if __name__ == "__main__":
    main()
