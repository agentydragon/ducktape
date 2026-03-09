/**
 * Bundles the mcporter CLI into a single self-contained CommonJS file.
 *
 * Run with: node bundle_mcporter.mjs <outfile>
 *
 * Uses esbuild to bundle node_modules/mcporter/dist/cli.js and all its
 * transitive dependencies (zod, commander, acorn, etc.) into one file so
 * the container only needs the Node.js binary — no node_modules directory.
 *
 * CJS format is used (not ESM) so Node.js runs it as a plain script
 * regardless of whether the containing directory has a package.json with
 * "type": "module".  The shebang is added via esbuild's banner option.
 */
import esbuild from 'esbuild';
import { resolve } from 'path';

const outfile = process.argv[2];
if (!outfile) {
  console.error('Usage: bundle_mcporter.mjs <outfile>');
  process.exit(1);
}

await esbuild.build({
  // 'mcporter/cli' follows the package exports map → dist/cli.js
  entryPoints: ['mcporter/cli'],
  bundle: true,
  platform: 'node',
  target: 'node22',
  format: 'cjs',
  outfile,
  // Add shebang so the file is directly executable with #!/usr/bin/env node
  banner: { js: '#!/usr/bin/env node' },
  // Follow Bazel symlinks in node_modules
  nodePaths: [resolve(process.cwd(), 'node_modules')],
  preserveSymlinks: false,
  logLevel: 'info',
});
