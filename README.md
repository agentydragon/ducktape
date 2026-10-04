# CI latency history

Reviewed CI latency reports and supporting evidence live on this dedicated branch.
Start at [index.html](index.html); immutable entries are under `runs/`, pinned to the
inspected devel source commit and observation window. Download/open HTML locally;
branch HTML is not a deployed GitHub Pages site.

The September 9 entry is a historical baseline copied unchanged from
`agentydragon-agent/ducktape` commit `6e940e419489cd482be7499dbed3adca4cca0bf2`.
The October 4 entry contains fresh measurements. Their workload mixes and coverage
differ; do not interpret them as a controlled before/after experiment.

Collection and publication tools remain on
[devel](https://github.com/agentydragon/ducktape/tree/devel/devinfra/ci/skills/ci_latency).
Append reviewed snapshots here through PRs against `ci-latency-history`. Keep raw
logs, profiles, webhook payloads and credentials out of this branch.
