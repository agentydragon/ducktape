// Produce independent evidence even when ordinary certification stops at the rename guard.
import path from "node:path";
import { constants, copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { createHash } from "node:crypto";

const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

const safeRoute = (name) =>
  !path.isAbsolute(name) && name.split("/").every((part) => part && part !== "." && part !== "..");

export function auditPlacement({ stage, chunkFile, specName, spec, statements, plan, placement, accept, run }) {
  const source = path.join(stage, chunkFile);
  const result = run(path.resolve(process.env.AUTHORED_PLACEMENT_CENSUS), [source, path.join(stage, "lean_spec.json")]);
  if (result.code !== 0 || result.failure) throw new Error(`placement census: ${result.failure ?? result.stderr}`);
  const proof = JSON.parse(result.stdout);
  const entryGlue = new Set((proof.entry_glue ?? []).map(([name]) => name));
  const sourcePublic = new Set(proof.source_exports.map(([, [name]]) => name));
  const publicLocals = new Set(proof.source_exports.map(([, [, local]]) => local));
  const functionNameSites = [];
  for (const { ordinal, members } of placement.units) {
    let node = statements[ordinal].node;
    if (node.type.startsWith("Export")) node = node.declaration;
    if (!node) continue;
    for (const [binding, readable] of Object.entries(members ?? {})) {
      if (binding === readable) continue;
      const declaration = ["FunctionDeclaration", "ClassDeclaration"].includes(node.type) && node.id?.name === binding;
      const init =
        node.type === "VariableDeclaration" &&
        node.declarations.find(({ id }) => id.type === "Identifier" && id.name === binding)?.init;
      const inferred =
        init &&
        (init.type === "ArrowFunctionExpression" ||
          (["FunctionExpression", "ClassExpression"].includes(init.type) && init.id === null));
      if (declaration || inferred) {
        functionNameSites.push({
          ordinal,
          binding,
          readable,
          kind: declaration ? node.type : init.type,
          in_original_namespace: publicLocals.has(binding),
        });
      }
    }
  }
  const renamePairs = placement.units.flatMap(({ members }) =>
    Object.entries(members ?? {}).filter(([first, second]) => first !== second)
  );
  const surface = observeRustSurface({
    stage,
    chunkFile,
    spec,
    accept,
    run,
    cases: renamePairs,
  });
  return {
    source: chunkFile,
    source_sha256: placement.source_sha256,
    spec: specName,
    spec_sha256: sha256(readFileSync(path.join(stage, specName))),
    assignment: placement.units,
    import_priority: plan.order,
    chunk_renames: spec.chunk_renames ?? {},
    source_types: statements.map(({ node }) =>
      node.type === "VariableDeclaration" ? `${node.type}:${node.kind}` : node.type
    ),
    function_name_sites: functionNameSites,
    rust_surface: surface,
    exports_asserted: accept.exports ?? {},
    extra_entry_cells: [...entryGlue].filter((name) => !sourcePublic.has(name)),
    ...proof,
  };
}

// The Rust fixture runner stages authored outputs under out/app/<chunk>.
// Keep this measurement separate from the ordinary certificate's files.
function observeRustSurface({ stage, chunkFile, spec, accept, run, cases }) {
  const name = chunkFile.replace(/\.js$/, "");
  const specFile = ".fixture/surface-spec.json";
  const entry = accept.entry ?? "main";
  if (!safeRoute(entry)) throw new Error(`invalid authored entry: ${entry}`);
  const output = path.join(stage, "census-output");
  const app = path.join(output, "app");
  const emitted = path.join(app, entry, "entry.js");
  writeFileSync(
    path.join(stage, specFile),
    JSON.stringify({
      ...spec,
      inputs: { input_root: ".", js_list_path: ".fixture/js-files.txt", ...spec.inputs },
      materialize_logical_modules: { target_dir: "modules", ...spec.materialize_logical_modules },
      write_js_tree: { ...spec.write_js_tree, out_dir: output },
    })
  );
  const rust = run(path.resolve(process.env.DEBUNDLE), ["run", "--spec", specFile], { cwd: stage });
  if (rust.code !== 0 || rust.failure) return { status: "emission_failed" };
  for (const [dest, source] of Object.entries(accept.runtime_files ?? {})) {
    if (!safeRoute(dest) || !safeRoute(source)) throw new Error(`invalid runtime file route: ${dest}: ${source}`);
    const file = path.join(app, dest);
    mkdirSync(path.dirname(file), { recursive: true });
    copyFileSync(path.join(stage, source), file, constants.COPYFILE_EXCL);
  }
  if (name !== entry) return { status: "unmapped_entry" };
  const script = `
    const [route, pairs] = process.argv.slice(1);
    const mod = await import(route);
    const functions = Object.keys(mod).flatMap((key) => {
      const value = mod[key];
      if (typeof value !== "function") return [];
      const d = Object.getOwnPropertyDescriptor(value, "name");
      return [[key, d && {
        kind: Object.hasOwn(d, "value") ? "data" : "accessor",
        value: typeof d.value === "string" ? d.value : null,
        writable: d.writable, enumerable: d.enumerable, configurable: d.configurable,
      }]];
    });
    const aliases = JSON.parse(pairs).filter(([old, readable]) => old in mod && readable in mod &&
      typeof mod[old] === "function" && typeof mod[readable] === "function")
      .map(([old, readable]) => [old, readable, mod[old] === mod[readable]]);
    console.log("SURFACE " + JSON.stringify({ names: Object.keys(mod), functions, aliases }));
  `;
  const pairs = JSON.stringify(cases);
  function observe(file) {
    const result = run(process.execPath, ["--input-type=module", "-e", script, pathToFileURL(file).href, pairs], {
      cwd: stage,
    });
    const line = result.stdout.split("\n").findLast((s) => s.startsWith("SURFACE "));
    return result.code === 0 && !result.failure && line ? JSON.parse(line.slice(8)) : null;
  }
  const original = observe(path.join(stage, chunkFile));
  const published = observe(emitted);
  if (!original || !published) {
    return { status: "import_failed", original, published };
  }
  const beforeNames = new Set(original.names);
  const afterNames = new Set(published.names);
  const afterFunctions = new Map(published.functions);
  return {
    status: "compared",
    original,
    published,
    added: published.names.filter((name) => !beforeNames.has(name)),
    removed: original.names.filter((name) => !afterNames.has(name)),
    changed_function_name_descriptors: original.functions
      .filter(
        ([name, descriptor]) =>
          afterFunctions.has(name) && JSON.stringify(descriptor) !== JSON.stringify(afterFunctions.get(name))
      )
      .map(([name]) => name),
  };
}
