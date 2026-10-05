// Build-time generator of a `previews` target's scenario table (util/testing/visual_scenarios.py):
// one scenario per fixture × color scheme × variant of one server's `PREVIEW_FIXTURES`. The
// fixtures module is the only list of fixtures; the table, and so every PNG name and manifest
// label, is derived from it here and read by the sweep (`py_visual_test`).
//
// Usage: node emit_scenarios.mjs <fixtures module .js> <scenario table .json>
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const COLOR_SCHEMES = ["light", "dark"];
const PREVIEW_VARIANTS = ["compact", "detailed"];

// Just needs to be wider than `.haku-shell-panels`'s `min(32rem, …)` cap — card.tsx renders the
// card inside that real class, so its own CSS (not this viewport) owns the card's width.
const VIEWPORT = { width: 1200, height: 900, deviceScaleFactor: 2 };

const [fixturesPath, tablePath] = process.argv.slice(2);
const { PREVIEW_FIXTURES } = await import(pathToFileURL(resolve(fixturesPath)).href);
if (!Array.isArray(PREVIEW_FIXTURES) || PREVIEW_FIXTURES.length === 0) {
  throw new Error(`${fixturesPath} does not export a non-empty PREVIEW_FIXTURES`);
}

function previewSlug(serverId, toolName) {
  return `${serverId}-${toolName}`
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

// Duplicate (serverId, toolName) pairs get -2, -3, … so every PNG name stays unique.
const seen = new Map();
const table = {};
// Fixture is the outer loop so each tool's compact/detailed × light/dark cluster together in the
// manifest (and thus on the review page).
for (const [index, { serverId, toolName }] of PREVIEW_FIXTURES.entries()) {
  const base = previewSlug(serverId, toolName);
  const count = seen.get(base) ?? 0;
  seen.set(base, count + 1);
  const slug = count === 0 ? base : `${base}-${count + 1}`;
  for (const colorScheme of COLOR_SCHEMES) {
    for (const variant of PREVIEW_VARIANTS) {
      table[`preview-${slug}-${variant}-${colorScheme}`] = {
        element: ".haku-preview-card",
        viewport: VIEWPORT,
        colorScheme,
        label: `${serverId} · ${toolName} — ${variant} · ${colorScheme}`,
        // A widget that fetches after mount (gmail subjects, grocy reference, calendar name) re-renders
        // inside the card; the sweep's network-ledger wait covers that, so the card is the whole condition.
        readySelectors: [".haku-preview-card"],
        windowGlobals: { __FIXTURE__: index, __VARIANT__: variant },
      };
    }
  }
}
writeFileSync(tablePath, `${JSON.stringify(table, null, 2)}\n`);
