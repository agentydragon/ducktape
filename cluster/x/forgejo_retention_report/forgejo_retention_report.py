"""Read-only Forgejo image retention report; never installs rules or deletes data.

Package/blob accounting comes from a repeatable-read PostgreSQL snapshot. This
depends on Forgejo's package schema, not its public API. Only dated devel tags of
the selected owner are candidates; all other versions, including OCI digest
records and in-progress uploads, retain their blob references.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REGISTRY = "git.allegedly.works"
TAG = re.compile(r"devel-[0-9]{14}-[0-9a-f]{7}")
TOKEN = re.compile(r"devel-[0-9]{14}-[0-9a-f]{7}|sha256:[0-9a-f]{64}")
REFERENCE = re.compile(re.escape(REGISTRY) + r"/([\w./-]+)[:@]([\w.:-]+)")
WORKLOADS = (
    "pods,deployments,statefulsets,daemonsets,replicasets,replicationcontrollers,"
    "jobs,cronjobs,podtemplates,sandboxtemplates.agentplane.allegedly.works,"
    "sandboxes.agentplane.allegedly.works,sandboxtemplates.extensions.agents.x-k8s.io,"
    "sandboxwarmpools.extensions.agents.x-k8s.io,virtualmachines.kubevirt.io,"
    "virtualmachineinstances.kubevirt.io"
)
SNAPSHOT_SQL = """
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '30s';
SELECT json_build_object(
  'captured_at', current_timestamp,
  'versions', (SELECT json_agg(json_build_object(
    'id', v.id, 'owner', u.name, 'name', p.name, 'type', p.type,
    'version', v.version, 'created', v.created_unix,
    'internal', v.is_internal OR p.is_internal))
    FROM package_version v JOIN package p ON p.id = v.package_id
    JOIN "user" u ON u.id = p.owner_id),
  'files', (SELECT json_agg(json_build_array(version_id, blob_id, is_lead)) FROM package_file),
  'blobs', (SELECT json_agg(json_build_object(
    'id', id, 'size', size, 'sha256', hash_sha256)) FROM package_blob)
);
COMMIT;
"""


def run(args: list[str], *, input_text: str | None = None, timeout: int = 180) -> str:
    return subprocess.run(args, input=input_text, text=True, capture_output=True, check=True, timeout=timeout).stdout


@dataclass
class Protection:
    tokens: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    references: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def scan(self, text: str, reason: str) -> None:
        # Split Kustomize newTag fields and image-tag metadata have no repository.
        # Protect matching dated tags/digests across repositories conservatively.
        for match in TOKEN.finditer(text):
            self.tokens[match.group()].add(reason)
        for match in REFERENCE.finditer(text):
            self.references[f"{match[1]}:{match[2]}"].add(reason)


def scan_repo(spec: str, rollback_days: int, protection: Protection) -> dict[str, Any]:
    path, separator, ref = spec.partition("=")
    repo = str(Path(path).resolve())
    ref = ref if separator else "HEAD"
    sha = run(["git", "-C", repo, "rev-parse", "--verify", f"{ref}^{{commit}}"]).strip()
    pattern = r"git[.]allegedly[.]works/|devel-[0-9]{14}-[0-9a-f]{7}|sha256:[0-9a-f]{64}"
    result = subprocess.run(
        ["git", "-C", repo, "grep", "-n", "-I", "-E", pattern, sha, "--", "."],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed for {repo}: {result.stderr}")
    for line in result.stdout.splitlines():
        _, filename, number, content = line.split(":", 3)
        protection.scan(content, f"git:{Path(repo).name}:{sha[:12]}:{filename}:{number}")
    if rollback_days:
        patch = run(
            [
                "git",
                "-C",
                repo,
                "log",
                sha,
                f"--since={rollback_days} days ago",
                "--format=",
                "--no-ext-diff",
                "--unified=0",
                "-p",
                "-G",
                pattern,
                "--",
                ".",
            ],
            timeout=300,
        )
        protection.scan(patch, f"rollback:{Path(repo).name}:{sha[:12]}:{rollback_days}d")
    return {
        "path": repo,
        "revision": sha,
        "origin": run(["git", "-C", repo, "remote", "get-url", "origin"]).strip(),
        "shallow": run(["git", "-C", repo, "rev-parse", "--is-shallow-repository"]).strip() == "true",
    }


def scan_workloads(objects: dict[str, Any], protection: Protection) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for obj in objects["items"]:
        metadata = obj["metadata"]
        kind = obj["kind"]
        counts[kind] += 1
        reason = f"live:{kind}:{metadata.get('namespace', '')}/{metadata['name']}"
        # Include image IDs, init/ephemeral containers, embedded Pod specs and env
        # values. Skip managedFields and annotations containing historical objects.
        for section in ("spec", "status", "template"):
            protection.scan(json.dumps(obj.get(section, {})), reason)
    return dict(counts)


def make_report(
    snapshot: dict[str, Any], protection: Protection, *, owner: str, keep: int, days: int, now: datetime
) -> dict[str, Any]:
    versions = {v["id"]: v for v in snapshot["versions"] or []}
    blobs = {b["id"]: b for b in snapshot["blobs"] or []}
    blob_refs: dict[int, set[int]] = defaultdict(set)
    version_blobs: dict[int, set[int]] = defaultdict(set)
    digests: dict[int, set[str]] = defaultdict(set)
    for version_id, blob_id, lead in snapshot["files"] or []:
        blob_refs[blob_id].add(version_id)
        version_blobs[version_id].add(blob_id)
        if lead:
            digests[version_id].add(f"sha256:{blobs[blob_id]['sha256']}")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for version in versions.values():
        groups[f"{version['owner']}/{version['name']}"].append(version)
    candidates: set[int] = set()
    details = []
    for image, group in sorted(groups.items()):
        public = sorted((v for v in group if not v["internal"]), key=lambda v: (v["created"], v["id"]), reverse=True)
        newest = {v["id"] for v in public[:keep]}
        for v in group:
            reasons: set[str] = set()
            if v["owner"] != owner or v["type"] != "container":
                reasons.add("outside-scope")
            if v["internal"]:
                reasons.add("internal")
            if not TAG.fullmatch(v["version"]):
                reasons.add("not-dated-devel-tag")
            if v["id"] in newest:
                reasons.add("newest-N")
            if v["created"] >= (now - timedelta(days=days)).timestamp():
                reasons.add("recent")
            reasons.update(protection.tokens.get(v["version"], set()))
            reasons.update(protection.references.get(f"{image}:{v['version']}", set()))
            for digest in digests[v["id"]]:
                reasons.update(protection.tokens.get(digest, set()))
                reasons.update(protection.references.get(f"{image}:{digest}", set()))
            if not reasons:
                candidates.add(v["id"])
            details.append(
                {
                    **v,
                    "image": image,
                    "candidate": not reasons,
                    "reasons": sorted(reasons),
                    "digests": sorted(digests[v["id"]]),
                    "referenced_bytes": sum(blobs[b]["size"] for b in version_blobs[v["id"]]),
                }
            )
    reclaimable = {b for b, refs in blob_refs.items() if refs and refs <= candidates}
    unreferenced = set(blobs) - blob_refs.keys()
    images = []
    for image, group in sorted(groups.items()):
        selected = {v["id"] for v in group} & candidates
        image_blobs = set().union(*(version_blobs[v["id"]] for v in group))
        freed_alone = {b for b in image_blobs if blob_refs[b] and blob_refs[b] <= selected}
        images.append(
            {
                "image": image,
                "versions": len(group),
                "candidates": len(selected),
                "unique_referenced_bytes": sum(blobs[b]["size"] for b in image_blobs),
                "reclaimable_alone_bytes": sum(blobs[b]["size"] for b in freed_alone),
            }
        )
    return {
        "captured_at": snapshot["captured_at"],
        "policy": {"owner": owner, "keep_newest": keep, "min_age_days": days},
        "summary": {
            "images": len(groups),
            "versions": len(versions),
            "candidate_versions": len(candidates),
            "package_blob_bytes": sum(b["size"] for b in blobs.values()),
            "candidate_unique_blob_bytes": sum(blobs[b]["size"] for b in reclaimable),
            "already_unreferenced_blob_bytes": sum(blobs[b]["size"] for b in unreferenced),
            "already_unreferenced_blobs": len(unreferenced),
        },
        "images": images,
        "versions": details,
    }


def gib(size: int) -> str:
    return f"{size / 1024**3:,.2f}"


def markdown(report: dict[str, Any]) -> str:
    s, policy = report["summary"], report["policy"]
    lines = [
        "# Forgejo retention report",
        "",
        f"Snapshot: {report['captured_at']}. Read-only; no deletions or rules installed.",
        "",
        f"Hypothetical policy: `{policy['owner']}` dated `devel-*` container tags only; keep newest "
        f"{policy['keep_newest']} per image and versions under {policy['min_age_days']} days, plus known references.",
        "",
        f"- {s['images']} images; {s['versions']} versions; {s['candidate_versions']} candidate versions.",
        f"- Package blobs: {gib(s['package_blob_bytes'])} GiB logical.",
        f"- Blobs referenced exclusively by candidates: {gib(s['candidate_unique_blob_bytes'])} GiB logical.",
        f"- Already unreferenced: {gib(s['already_unreferenced_blob_bytes'])} GiB "
        f"({s['already_unreferenced_blobs']} blobs).",
        "",
        "These bytes are deduplicated across all owners and package types. They exclude LFS, attachments, "
        "Actions artifacts/logs, storage replication, and SeaweedFS volume garbage. They are database-accounted "
        "logical bytes, not a measurement of immediately recoverable disk space.",
        "",
        "## Coverage and limitations",
        "",
        "- All listed live workload kinds were read cluster-wide, including dormant templates and old ReplicaSets.",
        "- Git scans use committed revisions; local uncommitted files and unlisted repositories are excluded.",
        "- Split tag fields are protected conservatively wherever a dated tag or digest appears, including docs/tests.",
        "- Internal records, digest-named versions, non-devel tags and other owners are retained.",
        "- Source, cluster and registry reads are separate snapshots. This is not authorization to delete.",
        "- External consumers and custom workload kinds beyond the listed kinds may add protection requirements.",
        "",
        "### Git revisions scanned",
        "",
    ]
    lines.extend(
        f"- `{repo['path']}` at `{repo['revision']}` (shallow: {repo['shallow']})." for repo in report["repos"]
    )
    lines += ["", f"Rollback history scanned: {report['rollback_days']} days.", "", "### Live workload kinds", ""]
    lines += [f"- `{kind}`: {count}" for kind, count in sorted(report["workload_counts"].items())]
    lines += ["", "### Flux Git sources (inventory; not automatically cloned)", ""]
    for source in report["flux_sources"]:
        lines.append(
            f"- `{source['name']}`: `{source['url']}` at `{source['revision']}` "
            f"(revision scanned: {source['revision_scanned']})."
        )
    lines += [
        "",
        "## Largest images by referenced blobs",
        "",
        "Images can share blobs, so these sizes must not be summed.",
        "",
        "| Image | Unique referenced (GiB) | Versions | Candidates |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| `{row['image']}` | {gib(row['unique_referenced_bytes'])} | {row['versions']} | {row['candidates']} |"
        for row in sorted(report["images"], key=lambda r: r["unique_referenced_bytes"], reverse=True)[:15]
    )
    lines += [
        "",
        "## Candidate images",
        "",
        "Per-image reclaimable bytes assume only that image's candidates are removed. Shared blobs "
        "released only by deleting candidates across multiple images appear only in the global total.",
        "",
        "| Image | Versions | Candidates | Reclaimable alone (GiB) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in sorted(report["images"], key=lambda r: r["reclaimable_alone_bytes"], reverse=True):
        if row["candidates"]:
            lines.append(
                f"| `{row['image']}` | {row['versions']} | {row['candidates']} | {gib(row['reclaimable_alone_bytes'])} |"
            )
    lines += [
        "",
        "## Old versions protected by references",
        "",
        "| Image | Version | Protection |",
        "| --- | --- | --- |",
    ]
    for version in report["versions"]:
        references = [r for r in version["reasons"] if r.startswith(("git:", "live:", "rollback:"))]
        if (
            version["owner"] == policy["owner"]
            and TAG.fullmatch(version["version"])
            and references
            and "recent" not in version["reasons"]
        ):
            lines.append(f"| `{version['image']}` | `{version['version']}` | {'; '.join(references[:3])} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-pod", required=True, help="Forgejo CNPG pod; local postgres peer authentication")
    parser.add_argument("--namespace", default="forgejo")
    parser.add_argument("--database", default="forgejo")
    parser.add_argument("--owner", default="ducktape-ci")
    parser.add_argument(
        "--repo", action="append", required=True, help="Git checkout PATH[=REV]; repeat for each desired/applied source"
    )
    parser.add_argument("--keep-newest", type=int, default=10)
    parser.add_argument("--min-age-days", type=int, default=30)
    parser.add_argument("--rollback-days", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True, help="Output path prefix (.json and .md)")
    args = parser.parse_args()
    if min(args.keep_newest, args.min_age_days, args.rollback_days) < 0:
        parser.error("retention parameters must be nonnegative")
    now = datetime.now(UTC)
    sources = json.loads(run(["kubectl", "get", "gitrepositories.source.toolkit.fluxcd.io", "-A", "-o", "json"]))
    print("Reading Forgejo package/blob snapshot...", file=sys.stderr)
    snapshot = json.loads(
        run(
            [
                "kubectl",
                "exec",
                "-i",
                "-n",
                args.namespace,
                args.db_pod,
                "-c",
                "postgres",
                "--",
                "psql",
                "-X",
                "-qAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-d",
                args.database,
            ],
            input_text=SNAPSHOT_SQL,
            timeout=60,
        )
    )
    protection = Protection()
    repos = []
    for spec in args.repo:
        print(f"Scanning Git source {spec}...", file=sys.stderr)
        repos.append(scan_repo(spec, args.rollback_days, protection))
    print("Reading live workload references and Flux sources...", file=sys.stderr)
    workloads = json.loads(run(["kubectl", "get", WORKLOADS, "-A", "-o", "json"]))
    counts = scan_workloads(workloads, protection)
    report = make_report(snapshot, protection, owner=args.owner, keep=args.keep_newest, days=args.min_age_days, now=now)
    scanned_revisions = {repo["revision"] for repo in repos}
    report.update(
        {
            "repos": repos,
            "rollback_days": args.rollback_days,
            "workload_counts": counts,
            "kube_context": run(["kubectl", "config", "current-context"]).strip(),
            "workload_resources": WORKLOADS,
            "flux_sources": [
                {
                    "name": f"{s['metadata']['namespace']}/{s['metadata']['name']}",
                    "url": s["spec"]["url"],
                    "revision": s.get("status", {}).get("artifact", {}).get("revision"),
                    "revision_scanned": s.get("status", {}).get("artifact", {}).get("revision", "").split(":")[-1]
                    in scanned_revisions,
                }
                for s in sources["items"]
            ],
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    args.output.with_suffix(".md").write_text(markdown(report))
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
