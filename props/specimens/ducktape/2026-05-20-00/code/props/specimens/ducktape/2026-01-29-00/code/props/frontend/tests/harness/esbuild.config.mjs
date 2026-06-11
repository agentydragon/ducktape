import esbuild from 'esbuild';
import esbuildSvelte from 'esbuild-svelte';
import tailwindcss from 'esbuild-plugin-tailwindcss';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const rootDir = resolve(__dirname, '../..');

const args = process.argv.slice(2);
const outdir = args[0] || resolve(__dirname, 'dist');

await esbuild.build({
  entryPoints: [resolve(__dirname, 'harness.ts')],
  bundle: true,
  outdir,
  format: 'esm',
  minify: false,
  sourcemap: true,
  target: ['es2022'],
  plugins: [
    esbuildSvelte({
      compilerOptions: {
        css: 'injected',
      },
    }),
    tailwindcss(),
  ],
  alias: {
    $lib: resolve(rootDir, 'src/lib'),
    $components: resolve(rootDir, 'src/components'),
  },
  // Help esbuild find node_modules in Bazel sandbox
  nodePaths: [resolve(process.cwd(), 'node_modules')],
  // Follow symlinks (required for Bazel's node_modules structure)
  preserveSymlinks: false,
  // Support svelte package exports condition
  conditions: ['svelte', 'browser', 'module', 'import'],
  logLevel: 'info',
});
