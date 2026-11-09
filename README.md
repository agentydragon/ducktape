# GitHub Pages

`gh-pages` is the publication branch for
<https://agentydragon.github.io/ducktape/>. It holds independent, latest-only
report snapshots. The CI reports are:

- **Latency:** [standalone HTML](latency/index.html), [evidence](latency/evidence.json),
  [source and observation window](latency/manifest.json).
- **Reliability:** [standalone HTML](reliability/index.html),
  [evidence](reliability/evidence.json), [source and observation window](reliability/manifest.json).
- **Visual reliability:** [standalone HTML](visual-reliability/index.html),
  [evidence](visual-reliability/evidence.json),
  [source and observation window](visual-reliability/manifest.json).

- **Cluster storage:** [current inventory and storage options](storage/index.html), a read-only live inventory snapshot with last-reported usage and migration alternatives.
- **Fixture and model audit:** [open findings](fixture-model-audit/index.html), with current acceptance criteria and source provenance.
- **Agentplane protocol shapes:** [open criteria](agentplane-protocol-shapes/index.html), with service ownership boundaries and pinned source evidence.

Each CI refresh replaces only its own directory's `index.html`, `evidence.json`,
`manifest.json`, and optional `attribution.json`, through a PR against `gh-pages`.
Preserve the other report directories and this navigation. Remove stale optional
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

`report.js` mounts `MantineProvider` with `defaultColorScheme="auto"` and Mantine's
`Typography` around the authored report HTML. Keep report-specific module scripts
outside `#report-content` and import the shared shell before attaching listeners.
This preserves filters and charts when React mounts. Only reviewed, committed HTML
belongs in the shell; never insert raw API responses or user input as HTML.
Report content remains present in the document if JavaScript/CDN loading fails.

The head sets the initial system color scheme; the provider follows subsequent
browser/OS changes. Keep custom CSS to layout, visualization and small native
control adjustments, using Mantine theme variables. Do not force light or dark.
Before publishing, render every affected page at desktop and mobile widths in both
themes, verify real CDN loading and live preference changes, and exercise its
controls. A presentation refresh must retain the original measurement/source
window and must not imply that old findings were remeasured.
