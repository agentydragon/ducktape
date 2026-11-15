# GitHub Pages

`gh-pages` is the publication branch for
<https://agentydragon.github.io/ducktape/>. It holds independent, latest-only
report snapshots. The CI reports are:

- **Agentplane test architecture:** [current branch plan](agentplane-test-architecture/index.html),
  [fixture ownership](agentplane-test-architecture/index.html#pytest-fixtures),
  [current evidence](agentplane-test-architecture/evidence.json),
  [source manifest](agentplane-test-architecture/manifest.json). All four native architecture ports are merged: explicit App fixtures (#16675), SVID clients (#16689), dependency-based suite partitions (#16695) and validated trust-Pod reuse (#16700). Compares the rebased draft #16489 with devel: only manual comparison targets and experiment records remain. Runtime JUnit confirms Pod 16+7 and MCP/central 1+3+4 case inventories. Historical selector, rejected shard and two/three/four-worker cohorts retain their original source pins. The separate four-worker port #16659 measured 289.2s versus 363.3s at two workers in four matched warm 22-case actions; a 30% complete-feedback improvement remains unqualified.

- **Latency:** [standalone HTML](latency/index.html), [evidence](latency/evidence.json),
  [source and observation window](latency/manifest.json). Original backend windows and denominators are retained; iteration 14 adds the shared-guest screen, targeted retry repair and VM-tail phases without a whole-CI saving claim.
- **Reliability (5–12 November failure history, VM OOM and merged-PR gates):** [standalone HTML](reliability/index.html),
  [evidence](reliability/evidence.json), [source and observation window](reliability/manifest.json). Iteration 14 adds the reproduced transient-rejection repair, 335 focused passing JUnit records and two fresh real Firecracker cases; original fleet rates and denominators remain unchanged. PR #16299 is merged, with its seven-day fresh-exposure follow-up and original controller exit cause still open.

- **Visual reliability:** [standalone HTML](visual-reliability/index.html),
  [evidence](visual-reliability/evidence.json),
  [source and observation window](visual-reliability/manifest.json).

- **Upstream patches and issues:** [reviewed inventory](upstream-patches/index.html), with upstream status, local impact, evidence, and follow-up actions.

- **Cluster storage:** [HDD budget scenarios, volume fullness and interventions](storage/index.html), with dated Langfuse, SeaweedFS, ClickHouse, Zot, Mimir and Loki evidence and the earlier inventory retained below it.
- **Fixture and model audit:** [open findings](fixture-model-audit/index.html), with current acceptance criteria and source provenance.
- **Agentplane organization:** [whole-Agentplane current and goal trees](agentplane-organization/index.html#trees), covering all 36 top-level areas and every one of 1,275 tracked Go files in 223 packages. Every package has a responsibility, goal owner and rationale; every file has a disposition. Six stages order the work by certainty: leftover deletions (including the Gateway's per-Sandbox Lease, its call epoch and the `-control` EndpointSlice), layout fixes that hold under any service map, agreed service moves (the Sandbox Controller to `environments/` as the Environments binary, then EnvironmentService, the Runtime proxy and the Executor into it; the rest of the Runner Gateway to `session_dispatcher/` as the Sessions binary; retiring orchestration_v1), local extractions, moves waiting on open service decisions (Notification split, the Dispatcher's ownership Lease, the Journal service, one settings source), and deferred contracts. The [target binaries](agentplane-organization/index.html#binaries) section records the agreed Environments/Sessions binary split, its cut line, keep-alive holds and the open operator questions. App has no go/ directory. [Remaining plan](agentplane-organization/go-plan.json); [file ledger](agentplane-organization/go-files.json); [source and validation evidence](agentplane-organization/evidence.json).
- **Agentplane protocol shapes:** [open criteria](agentplane-protocol-shapes/index.html), with service ownership boundaries and pinned source evidence.

Each CI refresh replaces its own directory's `index.html`, `evidence.json`,
`manifest.json`, and optional `attribution.json`, through a PR against `gh-pages`.
Preserve the other report directories; keep this navigation and the root index
current when report entries change. Remove stale optional
attribution only within the report being refreshed. Use Git history for earlier
measurements; do not accumulate dated run directories. This branch continues the
history of `ci-latency-history`; pre-migration latency snapshots are at the root
of their historical commits. GitHub Pages serves this branch at
<https://agentydragon.github.io/ducktape/> (root `index.html` lists the reports;
`.nojekyll` keeps Pages from processing the files).

Collection and publication tools remain on
[devel](https://github.com/agentydragon/ducktape/tree/devel/devinfra/ci/skills/ci_latency).
Keep raw logs, profiles, webhook payloads and credentials out of this branch.

## Presentation convention

Use **Mantine 9.6.1**, the version resolved on the inspected devel
branch, with React / React DOM 19.3.0. Use the version-pinned stylesheet from
jsDelivr (with its integrity hash) and the pinned esm.sh import map already in each
page. Reuse the shared `report.js` and `report.css`; do not introduce a second
framework or copy a report-specific design system. Update the entire site together
when changing the common framework. The former root used Pico; Mantine is now the
site-wide convention.

Author explicit React/Mantine components using the pinned `htm` tagged-template
library, imported through the shared `report.js`. Call `mount(Report)` with the
report component; the shared provider uses `defaultColorScheme="auto"`. Use normal
Mantine components for layout, cards, typography, controls, tables and disclosures.
Keep interactive state in React. Do not add a runtime HTML-to-component adapter,
duplicate report markup or reimplement component styling in shared CSS.

Use **Mantine Charts 9.6.1** for ordinary statistical charts, with its pinned
stylesheet after core styles and the existing pinned Recharts import-map entry.
Keep counts and units visible, provide accessible text or tables for the values,
and retain observation windows and denominators. Preserve useful visualizations
when refreshing a report. Custom SVG is appropriate for nested wall-time span
timelines; their intervals overlap and are not ordinary additive data series.

The head sets the initial system color scheme; the provider follows subsequent
browser/OS changes. Shared CSS contains only the span timeline's SVG styles;
use component props for layout and appearance. Do not force light or dark.
Before publishing, render every affected page at desktop and mobile widths in both
themes, verify real CDN loading and live preference changes, and exercise its
controls. Inspect the screenshots for meaningful bars, readable labels and retained
visual distinctions; successful browser assertions alone are insufficient.
A presentation refresh must retain the original measurement/source
window and must not imply that old findings were remeasured.
