// Recreate the public library inputs and their ESTree files for the lowering and split experiments.
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const [directory, ...requested] = process.argv.slice(2);
if (!directory || !path.isAbsolute(directory))
  throw new Error("usage: prepare_real_bundles <absolute output dir> [bundle ...]");
const stems = ["lodash", "acorn", "zod", "semver"].flatMap((name) => [name, `${name}.min`]);
await mkdir(directory, { recursive: true });
for (const variant of requested.length ? requested : stems) {
  if (!stems.includes(variant)) throw new Error(`unknown bundle: ${variant}`);
  const name = variant.replace(/\.min$/, "");
  const minify = variant !== name;
  const stem = path.join(directory, variant);
  const entry = new URL(`../lowering/entries/${name}.mjs`, import.meta.url);
  const { outputFiles } = await build({
    stdin: {
      contents: await readFile(entry, "utf8"),
      resolveDir: fileURLToPath(new URL(".", import.meta.url)),
      sourcefile: `${name}.mjs`,
    },
    bundle: true,
    format: "esm",
    platform: "node",
    target: "es2022",
    minify,
    write: false,
  });
  const source = outputFiles[0].text;
  await writeFile(`${stem}.mjs`, source);
  await writeFile(`${stem}.json`, JSON.stringify(lowerDump(source, acorn, eslintScope)));
}
