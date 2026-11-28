import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { parse } from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../../translation/scope_metadata.mjs";

function record({ ast }, importer) {
  const name = (id) => id?.name ?? id?.value;
  return topLevelStatements(ast).flatMap(({ node }, ordinal) => {
    const { type, source, specifiers, declaration } = node;
    if (source) {
      const target = path.posix.join(path.posix.dirname(importer), source.value);
      return [
        {
          ordinal,
          type,
          request: source.value,
          target,
          bindings: specifiers?.map((sp) =>
            type === "ImportDeclaration"
              ? [sp.type === "ImportNamespaceSpecifier" ? "*" : (name(sp.imported) ?? "default"), name(sp.local)]
              : [name(sp.local), name(sp.exported)]
          ),
        },
      ];
    }
    if (type === "ExportNamedDeclaration" && !declaration)
      return [{ ordinal, type, bindings: specifiers.map(({ local, exported }) => [name(local), name(exported)]) }];
    if (type === "ExportNamedDeclaration" || type === "ExportDefaultDeclaration") {
      const locals = (declaration.declarations ?? [declaration]).map(({ id }) => name(id));
      const exported = type === "ExportDefaultDeclaration" ? ["default"] : locals;
      return [{ ordinal, type, declaration: declaration.type, exported, operand: name(declaration) }];
    }
    return [];
  });
}

// Static synchronous order: keep all requests (including re-exports) in
// source order, but enter each record once.
function schedule(entry, records) {
  const order = [];
  const cycles = [];
  const active = [];
  const visited = new Set();
  function visit(file) {
    const open = active.indexOf(file);
    if (open >= 0) cycles.push([...active.slice(open), file]);
    if (visited.has(file)) return;
    visited.add(file);
    active.push(file);
    assert(records[file], file);
    for (const { target } of records[file]) if (target) visit(target);
    active.pop();
    order.push(file);
  }
  visit(entry);
  assert.equal(order.length, Object.keys(records).length);
  const requests = {};
  for (const { type, target } of Object.values(records).flat()) {
    if (target) requests[type] = (requests[type] ?? 0) + 1;
  }
  return { order, cycles, requests, records };
}

export function inspect({ original, candidate, wrongDependency, manifest, worker, observedNamespaceKeys }) {
  const { entry, chunks } = JSON.parse(readFileSync(manifest, "utf8"));
  const changed = chunks.find(({ spec }) => spec);
  const spec = JSON.parse(readFileSync(changed.spec, "utf8"));
  const parseTree = (root, files) =>
    Object.fromEntries(
      files.map((file) => [file, parseModule(readFileSync(path.join(root, file), "utf8"), parse, eslintScope)])
    );
  const paths = chunks.map(({ path }) => path);
  const before = parseTree(original, paths);
  const after = parseTree(candidate, [...paths, worker]);
  const annotations = (tree) =>
    Object.fromEntries(
      Object.entries(tree).map(([file, parsed]) => [file, dumpParsed(parsed, { includeOpaqueCaptures: true })])
    );
  const asts = annotations(before);
  const fixed = Object.fromEntries(paths.filter((file) => file !== changed.path).map((file) => [file, asts[file]]));
  const files = annotations({ [changed.path]: after[changed.path], [worker]: after[worker] });
  const modules = Object.entries({ ...asts, [worker]: files[worker], candidateFacade: files[changed.path] });
  const queries = [
    [entry, "defaultErrorMap"],
    ["v3/external.js", "defaultErrorMap"],
    ["v3/errors.js", "defaultErrorMap"],
    [entry, "z"],
    [entry, "default"],
  ];
  const namespaceInput = (trees) => ({
    entry,
    files: trees,
    namespace: "z",
    queries: [
      ["v3/locales/en.js", "default"],
      ["v3/errors.js", "defaultErrorMap"],
      ["v3/helpers/parseUtil.js", "defaultErrorMap"],
    ],
  });
  const wrongWorker = annotations(parseTree(wrongDependency, [worker]))[worker];
  const inputs = [
    { kind: "importMotion", input: { entry, spec, before: asts, reread: { ...asts, ...files } } },
    {
      kind: "importMotion",
      input: { entry, spec, before: asts, reread: { ...asts, ...files, [worker]: wrongWorker } },
    },
    { kind: "namespaceLinks", input: namespaceInput(asts) },
    { kind: "namespaceLinks", input: namespaceInput({ ...asts, ...files }) },
    { kind: "starMetadata", input: { files: asts, queries } },
    { kind: "starMetadata", input: { files: { ...asts, ...files }, queries } },
    { kind: "deploymentDiagnostic", input: { entry, source: asts[changed.path], spec, fixed, files } },
    ...modules.map(([key, ast]) => ({
      kind: "moduleDiagnostic",
      input: { path: key === "candidateFacade" ? changed.path : key, ast },
    })),
  ];
  const results = JSON.parse(
    execFileSync(process.env.LINKED_INPUT_CHECKER, [], {
      input: JSON.stringify(inputs) + "\n",
      encoding: "utf8",
      maxBuffer: 64 * 1024 * 1024,
    })
  );
  const [motionResult, rejectedImport, ...remainder] = results;
  assert.match(rejectedImport.error, /named bindings/);
  for (const row of [motionResult, ...remainder]) assert.ok(row.output, JSON.stringify(row));
  const [motion, beforeCells, afterCells, beforeStar, afterStar, deployment, ...probes] = [
    motionResult,
    ...remainder,
  ].map(({ output }) => output);
  assert.equal(motion.requests, 2);
  assert.equal(motion.concrete.error, "v3/ZodError.js: unsupported global Object");
  const starOrigins = [
    [changed.path, "default"],
    [changed.path, "default"],
    [changed.path, "default"],
    [entry, "z"],
    [entry, "default"],
  ];
  assert.deepEqual(
    beforeStar,
    starOrigins.map((output) => ({ output }))
  );
  assert.deepEqual(afterStar, beforeStar);
  function compareNamespaceCells({ target, members, exports, locals, namespaceBinding }) {
    assert.equal(target, "v3/external.js");
    assert.deepEqual(members.map(([name]) => name).sort(), observedNamespaceKeys);
    const cells = Object.fromEntries(members);
    const publicCells = Object.fromEntries(exports);
    for (const [name, cell] of members) assert.deepEqual(publicCells[name], cell, name);
    assert.deepEqual(publicCells.z, namespaceBinding);
    assert.notDeepEqual(publicCells.default, namespaceBinding);
    for (const [binding, cell] of locals) assert.deepEqual(cells.defaultErrorMap, cell, binding.join(":"));
    return {
      target,
      keys: members.length,
      defaultErrorMap: cells.defaultErrorMap,
      namespaceBinding,
      defaultSnapshotCell: publicCells.default,
    };
  }
  const namespaceCells = [beforeCells, afterCells].map(compareNamespaceCells);
  const graph = (tree) =>
    schedule(entry, Object.fromEntries(Object.entries(tree).map(([file, parsed]) => [file, record(parsed, file)])));
  const report = {
    deployment,
    motion,
    rejectedImport: rejectedImport.error,
    namespaceCells,
    starOrigins: queries.map((query, i) => ({ query, origin: starOrigins[i] })),
    before: graph(before),
    after: graph(after),
    modules: Object.fromEntries(modules.map(([key], index) => [key, probes[index]])),
  };
  const reportFile = path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "zod-v3-linked.json");
  writeFileSync(reportFile, JSON.stringify(report, null, 2) + "\n");
  console.log(
    JSON.stringify({
      deployment,
      motion,
      namespaceCells,
      starOrigins: report.starOrigins,
      before: report.before.order,
      after: report.after.order,
      cycles: [report.before.cycles, report.after.cycles],
      requests: [report.before.requests, report.after.requests],
      modules: Object.fromEntries(
        Object.entries(report.modules).map(([file, { module, units }]) => [
          file,
          {
            module,
            lowered: units.filter(({ kind }) => kind === "lowered").length,
            units: units.length,
            firstRefusal: units.find(({ loweringRefusal }) => loweringRefusal),
            firstOuterCallableRefusal: units.find(({ outerCallableRefusal }) => outerCallableRefusal),
          },
        ])
      ),
    })
  );
}
