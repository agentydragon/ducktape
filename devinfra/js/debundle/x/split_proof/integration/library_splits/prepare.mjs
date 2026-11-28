// Recreate the public library inputs and their ESTree files for the lowering and split experiments.
import assert from "node:assert/strict";
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule } from "../../translation/scope_metadata.mjs";

const [output, ...args] = process.argv.slice(2);
const provenance = args[0] === "--provenance";
const requested = provenance ? args.slice(1) : args;
if (!output) throw new Error("usage: prepare <output dir> [--provenance] [bundle ...]");
const directory = path.resolve(output);
const entries = new URL("./entries/", import.meta.url);
const stems = (await readdir(entries))
  .filter((file) => file.endsWith(".mjs"))
  .sort()
  .flatMap((file) => {
    const name = path.basename(file, ".mjs");
    return [name, `${name}.min`];
  });
await mkdir(directory, { recursive: true });
for (const variant of requested.length ? requested : stems) {
  if (!stems.includes(variant)) throw new Error(`unknown bundle: ${variant}`);
  const name = variant.replace(/\.min$/, "");
  const minify = variant !== name;
  const stem = path.join(directory, variant);
  const entry = new URL(`${name}.mjs`, entries);
  const options = {
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
  };
  const { outputFiles } = await build(options);
  if (provenance) {
    // External maps do not append a sourceMappingURL. Keep the virtual output
    // in the working directory so map and metafile paths have the same base.
    const mapped = await build({ ...options, outfile: "bundle.mjs", sourcemap: "external", metafile: true });
    const js = mapped.outputFiles.find((file) => file.path.endsWith("/bundle.mjs"));
    const map = mapped.outputFiles.find((file) => file.path.endsWith("/bundle.mjs.map"));
    assert.deepEqual(js.contents, outputFiles[0].contents, `${variant}: sidecars changed the input bytes`);
    await writeFile(`${stem}.map`, map.text);
    await writeFile(`${stem}.meta.json`, JSON.stringify(mapped.metafile));
  }
  const source = outputFiles[0].text;
  await writeFile(`${stem}.mjs`, source);
  await writeFile(`${stem}.json`, JSON.stringify(dumpParsed(parseModule(source, acorn.parse, eslintScope))));
}
