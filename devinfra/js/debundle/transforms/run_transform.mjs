#!/usr/bin/env node

import { requireValue } from "../common/io.mjs";
import { runTransformSpec } from "./runner.mjs";

async function main() {
  const { packageRoots, packagesRoot, specPath } = parseArgs(process.argv.slice(2));
  const result = await runTransformSpec(specPath, { packageRoots, packagesRoot });
  process.stdout.write(
    `Ran ${result.steps.length} transform steps from ${result.specPath} in ${formatDuration(result.durationMs)}\n`
  );
  for (const step of result.steps) {
    process.stdout.write(
      `- ${step.id}: ${step.operation} (${formatDuration(step.durationMs ?? 0)})${
        step.manifestKind ? ` [${step.manifestKind}]` : ""
      }\n`
    );
  }
}

export function parseArgs(argv) {
  const packageRoots = Object.create(null);
  let packagesRoot = null;
  let specPath = null;
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    if (arg === "--spec") {
      specPath = requireValue(argv, ++index, arg);
    } else if (arg === "--package-root") {
      const { packageName, packageRoot } = parsePackageRootArg(requireValue(argv, ++index, arg), arg);
      packageRoots[packageName] = packageRoot;
    } else if (arg === "--packages-root") {
      packagesRoot = requireValue(argv, ++index, arg);
    } else if (arg === "--help" || arg === "-h") {
      process.stdout.write(`Usage:
  run_transform --spec <spec.jsonc> [--package-root <pkg>=<dir>]... [--packages-root <dir>]

Runs the JavaScript transform pipeline described by the spec. Pipeline stages
dispatch directly to registered functions; this target does not invoke Bazel
from inside the pipeline. Specs are parsed as JSON with comments.
`);
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  if (specPath === null) {
    throw new Error("--spec is required");
  }
  return {
    packageRoots: Object.keys(packageRoots).length > 0 ? packageRoots : null,
    packagesRoot,
    specPath,
  };
}

function parsePackageRootArg(value, flag) {
  const separator = value.indexOf("=");
  if (separator <= 0 || separator === value.length - 1) {
    throw new Error(`${flag} must be in <package>=<dir> form, got ${value}`);
  }
  return {
    packageName: value.slice(0, separator),
    packageRoot: value.slice(separator + 1),
  };
}

function formatDuration(durationMs) {
  if (durationMs >= 1000) {
    return `${(durationMs / 1000).toFixed(3)}s`;
  }
  return `${durationMs.toFixed(3)}ms`;
}

await main();
