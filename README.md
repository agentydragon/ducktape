# Latest CI latency report

This dedicated branch holds the latest reviewed CI latency snapshot:

- [Report (standalone HTML)](index.html)
- [Supporting evidence](evidence.json)
- [Inspected source commit and observation window](manifest.json)
- Optional `attribution.json` when cost attribution was collected

Each refresh replaces these root files in a new commit through a PR against
`ci-latency-history`. Use Git history for earlier measurements; do not accumulate
run directories in the current tree. Download/open HTML locally; this branch is
not a deployed GitHub Pages site.

Collection and publication tools remain on
[devel](https://github.com/agentydragon/ducktape/tree/devel/devinfra/ci/skills/ci_latency).
Keep raw logs, profiles, webhook payloads and credentials out of this branch.
