import { build } from "esbuild";
for (const name of ["lodash", "acorn", "zod", "semver"]) {
  for (const minify of [false, true]) {
    await build({
      entryPoints: [`entries/${name}.mjs`],
      bundle: true,
      format: "esm",
      platform: "node",
      target: "es2022",
      minify,
      outfile: `bundles/${name}${minify ? ".min" : ""}.mjs`,
      logLevel: "error",
    });
  }
}
