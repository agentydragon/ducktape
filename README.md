# GitHub Pages

`gh-pages` is the publication branch for
<https://agentydragon.github.io/ducktape/>. It holds independent, latest-only
report snapshots. The CI reports are:

- **Agentplane test architecture:** [fast-CI plan, implementation progress and recovery measurements](agentplane-test-architecture/index.html),
  [evidence](agentplane-test-architecture/evidence.json),
  [source manifest](agentplane-test-architecture/manifest.json). Source 8a555 passes all three required checks (351s in one workflow; 59 fresh actions / 292 passing cases). The manual packaged guest Session contract completes two real Shell turns through History with retained state and a valid wrong-identity control. The 4/8/8/4 Bridge probe remains a selected-target scheduling candidate with higher CPU/capacity cost. The observer repair has live proof but is unshipped; bytecode collection has a substantial cold compile cost. Historical replay and whole-PR speed qualification remain open; the devel merge gate is unchanged.
- **Latency:** [standalone HTML](latency/index.html), [evidence](latency/evidence.json),
  [source and observation window](latency/manifest.json). Original backend windows and denominators are retained; the supplement adds the 8a555 required-CI observation, bytecode collection cost and bounded setup attribution. No whole-CI saving is established.
- **Reliability (5–12 November failure history, VM OOM and merged-PR gates):** [standalone HTML](reliability/index.html),
  [evidence](reliability/evidence.json), [source and observation window](reliability/manifest.json). A live injected observer error preserved the single accepted Run and exit 23, with no cancellation or second submission. The tested repair remains unshipped. Manual guest contracts and source CI are separate from the retained reliability population; original rates remain unchanged.

- **Visual reliability:** [standalone HTML](visual-reliability/index.html),
  [evidence](visual-reliability/evidence.json),
  [source and observation window](visual-reliability/manifest.json).

- **Upstream patches and issues:** [reviewed inventory](upstream-patches/index.html), with upstream status, local impact, evidence, and follow-up actions.

- **Cluster storage:** [HDD budget scenarios, volume fullness and interventions](storage/index.html), with dated Langfuse, SeaweedFS, ClickHouse, Zot, Mimir and Loki evidence and the earlier inventory retained below it.
- **Fixture and model audit:** [open findings](fixture-model-audit/index.html), with current acceptance criteria and source provenance.
- **Agentplane organization:** [whole-Agentplane current and goal trees](agentplane-organization/index.html#trees), covering all 36 top-level areas and every one of 1,253 tracked Go files in 219 packages. Each component has a source-backed goal decision; the remaining plan distinguishes exact placement moves, candidate extractions and deferred contract design. App has no go/ directory. [Remaining plan](agentplane-organization/go-plan.json); [file ledger](agentplane-organization/go-files.json); [source and validation evidence](agentplane-organization/evidence.json).
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
