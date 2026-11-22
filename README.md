# GitHub Pages

`gh-pages` is the publication branch for
<https://agentydragon.github.io/ducktape/>. It holds independent, latest-only
report snapshots. The [report index](index.html) links each review and its observation window.

- **Agentplane test architecture:** [current required suites](agentplane-test-architecture/index.html), [fixture ownership](agentplane-test-architecture/index.html#pytest-fixtures), [case inventory and evidence](agentplane-test-architecture/evidence.json), [manifest](agentplane-test-architecture/manifest.json). Current Pod coverage spans 15 App scenarios, eight native platform checks and one native workspace replacement. Earlier benchmarks retain their original test inventories and revisions.

- **Bazel target and wiring audit:** [source review](bazel-audit/) and [query results](bazel-audit/evidence.json). At d1402061ea, all 329 tracked frontend TypeScript files reach the typed-lint group. A simulated package omission drops six.

- **CI test cost:** [test and fixture audit](ci-test-cost/index.html), with fresh Action and Thread browser costs, devel push controls, and a current dependency graph probe. [Evidence](ci-test-cost/evidence.json) · [Manifest](ci-test-cost/manifest.json)

- **Latency (15–21 November UTC; partial 22 November):** [report](latency/index.html). 126 of 168 sampled heads reached required green. Median 3m 06s, p90 9m 36s. The report covers the Bazel gate replay, KVM clock rollout and closed KVM pilots. [Evidence](latency/evidence.json) · [Manifest](latency/manifest.json)

- **Reliability (15–21 November UTC; partial 22 November):** [report](reliability/index.html). Thirteen of 168 sampled heads remained red. The separate 22 November sample includes one verified fresh sandbox frontend recovery and four fresh first-attempt VM passes. [Evidence](reliability/evidence.json) · [Manifest](reliability/manifest.json)

- **Visual reliability:** [report](visual-reliability/index.html), with a visual operation geometry failure, measured visual History cache exposure and current Chart.js patch applicability. [Evidence](visual-reliability/evidence.json) · [Manifest](visual-reliability/manifest.json)

- **Upstream patches and issues:** [reviewed inventory](upstream-patches/index.html), with 33 reviewed items, the merged Gazelle 2.5.1 pins, and measured Grocy and Ruff workaround checks.

- **Cluster storage (22 November UTC):** [HDD capacity and interventions](storage/index.html). Three OVH HDD pools gained 477 GB of physical allocation in a week. Declared limits exceed physical capacity on two pools. [Volume ownership](storage/#ownership-review) traces the two idle capture claims. [Zot discard](storage/#zot-discard) compares the live mount with physical allocation on both copies. [Current evidence](storage/current-evidence.json) · [Earlier analysis](storage/index.html#historical-analysis) · [Manifest](storage/manifest.json).
- **Cluster VPA (22 November UTC):** [Coverage and rollout checks](vpa/index.html). 215 of 231 active controllers have a direct VPA. A CNPG switchover test recorded an 11.1-second client-read gap; the GitLab Rails capacity gate remains open. [Inventory](vpa/inventory.json) · [Rollout evidence](vpa/rollout-evidence.json) · [Manifest](vpa/manifest.json).
- **Fixture and model audit:** [current findings](fixture-model-audit/index.html), [evidence](fixture-model-audit/evidence.json), [manifest](fixture-model-audit/manifest.json). Priorities are the Python App adapters and a small shared Egress Relay settings corpus.
- **Agentplane organization:** [current and goal trees](agentplane-organization/index.html#trees), [remaining plan](agentplane-organization/go-plan.json), [complete Go file ledger](agentplane-organization/go-files.json), [manifest](agentplane-organization/manifest.json). Includes the [keep-alive hold proposal](agentplane-organization/index.html#keep-alive).
- **Agentplane protocol shapes:** [current findings](agentplane-protocol-shapes/index.html), [evidence](agentplane-protocol-shapes/evidence.json), [manifest](agentplane-protocol-shapes/manifest.json). Session override presence and a focused settings comparison are the priority.

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
For reliability snapshots, set the HTML’s `evidenceSha` to the final
`evidence.json` SHA-256 in `manifest.json`. The report uses it in the
request URL and verifies the response before rendering.

## Reviewing paired VM runs

Automatic managed TCG shadow admission was disabled on 20 November 2026 at 07:36 UTC. The archived comparison includes the 12-pair and 100-pair manual batches and the two devel cohorts on either side of the KVM routing cutover. Required VM tests run on recycled cluster KVM. [Per-pair evidence](vm-pairs.json) · [KVM routing PR](https://github.com/agentydragon/ducktape/pull/18046) · [Shadow removal PR](https://github.com/agentydragon/ducktape/pull/18079)

The [original collector](https://github.com/agentydragon/ducktape/pull/17940) launched the recycled KVM shadow after a fresh managed result. Version 1 archives store KVM results in `shadow/`. After the cutover, version 2 launched the managed TCG shadow after a fresh required KVM result. Its `pair.json` records `required_route` and `shadow_route`, and its physical routes use `managed/` and `kvm/`.

Historical artifacts were named `vm-pair-RUN_ID-RUN_ATTEMPT` and requested 90 days of retention. For a review of an archived pair, join on run ID, attempt, and tested SHA. The target, execution and case records distinguish cached results, executor retries, Bazel retries and failed first attempts. The archived reports use native queue clocks only when enqueue and worker-start timestamps are ordered. [Version 1 run](https://github.com/agentydragon/ducktape/actions/runs/44660636046) · [Version 2 fresh pair](https://github.com/agentydragon/ducktape/actions/runs/44688973875/artifacts/14607984419)
