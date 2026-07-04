"""Evacuate a flat SeaweedFS volume server onto the `hdd` volumeTopology group.

Stage 1 Phase 2 of the OVH storage tiering migration
(cluster/docs/plans/ovh_storage_tiering.md). Moves every volume off a flat
`seaweedfs-volume-N` server onto the `seaweedfs-volume-hdd-*` servers with
`weed shell volume.move`, which copies → tails-for-in-flight → deletes the
source copy, so the volume never drops below 2 copies. Targets are chosen so a
volume's two copies never share a node (replication `001` = 2 copies, 2 nodes,
1 rack).

Run ONE flat server at a time and confirm G-swfs green (`--check`) between
servers. Idempotent: a re-run after an interruption resumes, since already-moved
volumes are no longer on the source and simply aren't planned.

Defaults to a dry-run; pass `--apply` to actually move.

    # preview, then evacuate, then check
    bazel run //cluster/scripts:seaweedfs_evacuate_flat_volume -- seaweedfs-volume-0
    bazel run //cluster/scripts:seaweedfs_evacuate_flat_volume -- seaweedfs-volume-0 --apply
    bazel run //cluster/scripts:seaweedfs_evacuate_flat_volume -- --check
"""

import argparse
import re
import subprocess
import time
from dataclasses import dataclass

NAMESPACE = "seaweedfs"
HDD_MARKER = "-hdd-"
VOLUME_PORT = 8444


@dataclass(frozen=True)
class Move:
    source: str  # source volume-server pod name
    target: str  # target hdd volume-server pod name
    volume_id: int


def _kubectl(args: list[str], stdin: str | None = None, retries: int = 3) -> str:
    """Run kubectl with stderr merged into stdout, retrying transient apiserver
    timeouts (etcd contention). `weed`'s glog status lines (`moved volume N`) go
    to stderr while progress goes to stdout, so callers need the merged stream.
    The 600s ceiling covers a full 16 GB volume.move over nebula."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["kubectl", "-n", NAMESPACE, *args],
                check=False,
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            raise SystemExit(f"kubectl {' '.join(args)} exceeded 600s; re-run to resume (idempotent)") from None
        if result.returncode == 0:
            return result.stdout
        if "Timeout" in result.stdout and attempt < retries - 1:
            time.sleep(3 * (attempt + 1))
            continue
        raise SystemExit(f"kubectl {' '.join(args)} failed:\n{result.stdout.strip()[-800:]}")
    raise SystemExit(f"kubectl {' '.join(args)} timed out after {retries} attempts")


def master_pod() -> str:
    out = _kubectl(
        [
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/component=master",
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ]
    )
    if not out.strip():
        raise SystemExit("no running seaweedfs master pod found")
    return out.strip()


def weed_shell(commands: list[str], master: str) -> str:
    stdin = "".join(f"{c}\n" for c in commands)
    return _kubectl(["exec", "-i", master, "--", "weed", "shell"], stdin=stdin)


def peer_address(pod: str) -> str:
    # <pod>.<statefulset>-peer.<ns>:8444; the StatefulSet name is the pod name
    # without its ordinal suffix (seaweedfs-volume-hdd-2 -> seaweedfs-volume-hdd).
    statefulset = re.sub(r"-\d+$", "", pod)
    return f"{pod}.{statefulset}-peer.{NAMESPACE}:{VOLUME_PORT}"


def server_nodes() -> dict[str, str]:
    """Map each volume-server pod name to the node it runs on."""
    out = _kubectl(
        [
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/component=volume",
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}={.spec.nodeName}{'\\n'}{end}",
        ]
    )
    nodes = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if not nodes:
        raise SystemExit("no seaweedfs volume-server pods found")
    return nodes


def volume_locations(master: str) -> dict[int, list[str]]:
    """volumeId -> list of volume-server pod names holding a copy."""
    locations: dict[int, list[str]] = {}
    server: str | None = None
    for line in weed_shell(["volume.list"], master).splitlines():
        if datanode := re.search(r"DataNode (seaweedfs-volume[\w-]*)\.", line):
            server = datanode.group(1)
        elif (vol := re.search(r"volume Id:(\d+),", line)) and server is not None:
            locations.setdefault(int(vol.group(1)), []).append(server)
    if not locations:
        raise SystemExit("volume.list returned no volumes")
    return locations


def plan(source: str, locations: dict[int, list[str]], nodes: dict[str, str]) -> list[Move]:
    hdd_servers = sorted(s for s in nodes if HDD_MARKER in s)
    if not hdd_servers:
        raise SystemExit("no hdd-topology volume servers found (run Phase 1 first)")
    # Balance by current volume count so evacuated volumes spread evenly.
    load = {h: sum(1 for copies in locations.values() if h in copies) for h in hdd_servers}

    moves: list[Move] = []
    for vid in sorted(v for v, copies in locations.items() if source in copies):
        copies = locations[vid]
        # Keep the volume's two copies on two different nodes: forbid the node(s)
        # of the copy that stays behind.
        forbidden = {nodes[c] for c in copies if c != source}
        candidates = [h for h in hdd_servers if nodes[h] not in forbidden and h not in copies]
        if not candidates:
            raise SystemExit(f"volume {vid}: no valid hdd target (copies on {copies})")
        target = min(candidates, key=lambda h: (load[h], h))
        load[target] += 1
        moves.append(Move(source=source, target=target, volume_id=vid))
    return moves


def apply_move(move: Move, master: str) -> None:
    command = (
        f"volume.move -source {peer_address(move.source)}"
        f" -target {peer_address(move.target)} -volumeId {move.volume_id}"
    )
    out = weed_shell(["lock", command, "unlock"], master)
    if f"moved volume {move.volume_id}" in out:
        return
    # The exec stream can truncate while the move still completes server-side, so
    # confirm authoritatively: the source no longer holds the volume.
    if move.source not in volume_locations(master).get(move.volume_id, []):
        return
    raise SystemExit(f"volume.move {move.volume_id} did not confirm:\n{out[-800:]}")


def under_replicated(master: str) -> int:
    # `volume.fix.replication` with no -apply is a dry-run; each reported line is
    # a volume missing a replica.
    out = weed_shell(["volume.fix.replication"], master)
    return sum(1 for line in out.splitlines() if "under replicated" in line)


def print_status(master: str, nodes: dict[str, str]) -> None:
    locations = volume_locations(master)
    counts: dict[str, int] = dict.fromkeys(nodes, 0)
    for copies in locations.values():
        for server in copies:
            counts[server] = counts.get(server, 0) + 1
    for server in sorted(counts):
        print(f"  {server:<26} {counts[server]:>4} volumes  ({nodes.get(server, '?')})")
    print(f"  under-replicated: {under_replicated(master)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", nargs="?", help="flat volume server to evacuate, e.g. seaweedfs-volume-0")
    parser.add_argument("--apply", action="store_true", help="execute the moves (default: dry-run)")
    parser.add_argument("--check", action="store_true", help="print per-server volume counts + G-swfs and exit")
    args = parser.parse_args()

    master = master_pod()
    nodes = server_nodes()

    if args.check:
        print_status(master, nodes)
        return

    if not args.source:
        parser.error("source volume server is required (or use --check)")
    if args.source not in nodes:
        parser.error(f"unknown volume server {args.source!r}; known: {sorted(nodes)}")
    if HDD_MARKER in args.source:
        parser.error("source must be a flat server, not an hdd-topology server")

    if (n := under_replicated(master)) != 0:
        raise SystemExit(f"G-swfs not green: {n} under-replicated volume(s); resolve before evacuating")

    moves = plan(args.source, volume_locations(master), nodes)
    if not moves:
        print(f"{args.source} already holds no volumes; nothing to do.")
        return

    verb = "Moving" if args.apply else "[dry-run] would move"
    print(f"{verb} {len(moves)} volume(s) off {args.source}:")
    for i, move in enumerate(moves, 1):
        print(f"  [{i}/{len(moves)}] volume {move.volume_id} -> {move.target} ({nodes[move.target]})")
        if args.apply:
            apply_move(move, master)

    if args.apply:
        remaining = sum(1 for copies in volume_locations(master).values() if args.source in copies)
        print(f"\n{args.source}: {remaining} volume(s) remaining; under-replicated: {under_replicated(master)}")
        if remaining:
            print("Re-run to move the remainder (some moves may have been skipped).")


if __name__ == "__main__":
    main()
