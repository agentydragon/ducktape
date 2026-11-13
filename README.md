# GitHub Pages

`gh-pages` is the publication branch for
<https://agentydragon.github.io/ducktape/>. It holds independent, latest-only
report snapshots. The CI reports are:

- **Agentplane test architecture:** [fast-CI plan, implementation progress and recovery measurements](agentplane-test-architecture/index.html),
  [evidence](agentplane-test-architecture/evidence.json),
  [source manifest](agentplane-test-architecture/manifest.json). Source 0a9 passes all three required checks. Eight shards are enabled on the architecture branch as a scheduling candidate: the target-only ABBA lowered selected-target elapsed 24.9% while increasing summed CPU 62.8% and estimated concurrent requests from 12 vCPU/30 GiB to 24 vCPU/60 GiB. No complete-CI saving or devel policy change is established. The ASGI import profile is collection-only; the BuildBuddy observer patch remains unshipped. Historical replay and quarter-payback qualification remain open; the devel merge gate is unchanged.
- **Latency:** [standalone HTML](latency/index.html), [evidence](latency/evidence.json),
  [source and observation window](latency/manifest.json). Refreshed devel-base backend feedback and ten dispatched lanes retain their original windows; the supplement adds a 4/8/8/4 Bridge probe and the green 0a9 required-CI observation, with its CPU/capacity tradeoff and no whole-CI attribution.
- **Reliability (5–12 November failure history, VM OOM and merged-PR gates):** [standalone HTML](reliability/index.html),
  [evidence](reliability/evidence.json), [source and observation window](reliability/manifest.json). The selected outer BuildBuddy observer patch passed 38 upstream tests plus a pre-patch negative control but has no live-service proof or released package. The listener repair is green on its backport PR; its seven-day fresh-exposure window starts after merge. Source 0a9 CI does not establish a reliability improvement; original rates remain unchanged.
- **Visual reliability:** [standalone HTML](visual-reliability/index.html),
  [evidence](visual-reliability/evidence.json),
  [source and observation window](visual-reliability/manifest.json).

- **Upstream patches and issues:** [reviewed inventory](upstream-patches/index.html), with upstream status, local impact, evidence, and follow-up actions.

- **Cluster storage:** [HDD budget scenarios, volume fullness and interventions](storage/index.html), with dated Langfuse, SeaweedFS, ClickHouse, Zot, Mimir and Loki evidence and the earlier inventory retained below it.
- **Fixture and model audit:** [open findings](fixture-model-audit/index.html), with current acceptance criteria and source provenance.
- **Agentplane organization:** [architecture, ownership inventory and staged package plan](agentplane-organization/index.html), with original ownership inventory at `93f2ba8eef`, reorganization preflight at `1f7caaa9dc` (implementation baseline; GitHub PR state observed 2026-11-13 10:40:29 UTC), 36 earlier tracked PRs merged; this set: 19 merged, 0 open; native stack #16183 is complete; separate CI repairs: #16180 merged, #16199 merged. [Evidence](agentplane-organization/evidence.json).
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
