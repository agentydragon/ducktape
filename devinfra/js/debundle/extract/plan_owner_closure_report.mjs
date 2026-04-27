#!/usr/bin/env node

import {
  extractOwnerClosurePlanReport,
  parseOwnerClosurePlanReportArgs,
} from "./closure_report.mjs";

function usage() {
  return `Usage:
  plan_owner_closure_report --input-root <renamed-root> --out <report-dir> [options]

Runs the disconnected ordered-init owner closure planner on a renamed JavaScript
snapshot root and writes per-chunk JSON reports plus a summary. This does not
modify the input snapshot and does not wire anything into the live transform
pipeline.

Options:
  --input-root <dir>       Renamed snapshot root to analyze
  --input-manifest <path>  Manifest to read (defaults to <input-root>/manifest.json)
  --out <dir>              Directory for per-chunk reports
  --summary <path>         Summary JSON path
  --chunk-id <chunk>       Restrict analysis to one chunk (repeatable)
  --ui-version <value>     Override uiVersion in the report
  --force                  Replace a non-empty output directory
`;
}

try {
  const options = parseOwnerClosurePlanReportArgs(process.argv.slice(2));
  if (options.help) {
    process.stdout.write(usage());
    process.exit(0);
  }
  const summary = extractOwnerClosurePlanReport(options);
  process.stdout.write(
    `Wrote owner closure report for ${summary.counts.chunks} chunk(s) to ${summary.outDir}\n`
  );
} catch (error) {
  process.stderr.write(`${error.stack ?? error.message}\n`);
  process.exitCode = 1;
}
