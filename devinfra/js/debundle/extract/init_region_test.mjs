import assert from "node:assert/strict";
import { join } from "node:path";
import test from "node:test";
import { analyzeRuntimeBoundaryCode } from "../analysis/boundary.mjs";
import { createTempFixtureRoot, runNodeScript, writeRunnableFixture } from "../test_support/fixtures.mjs";
import { extractOrderedInitRegionsInCode } from "./init_region.mjs";

test("extracts an ordered-init region and preserves executable behavior", () => {
  const source = `import { addOne } from "./helpers.js";

const seed = format(addOne(1));
function format(value) {
  return \`value=\${value}\`;
}
class Box {
  constructor(value) {
    this.value = value;
  }
  render() {
    return format(this.value);
  }
}
async function loadFeature() {
  const mod = await import("./feature.js");
  return mod.describe(seed);
}
const result = new Box(seed).render();
const asyncValue = await loadFeature();
console.log(result, asyncValue, Box.name);
export { Box as publicBox, loadFeature as publicLoadFeature, result as publicResult };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    orderedInitOperation(source, {
      init: "init_fixture_region",
      ownerNames: ["seed", "format", "Box", "loadFeature", "result"],
      targetFile: "regions/fixture_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/fixture_region.js");
  assert.match(runtimeCode, /import \{ seed, format, Box, loadFeature, result, init_fixture_region_stage_0 \} from "\.\/regions\/fixture_region\.js"/);
  assert.match(runtimeCode, /init_fixture_region_stage_0\(\);/);
  assert.match(extractedCode, /import \{ addOne \} from "\.\.\/helpers\.js"/);
  assert.match(extractedCode, /await import\("\.\.\/feature\.js"\)/);
  assert.match(extractedCode, /format = function format/);

  assertRunnableEquivalent({
    files: {
      "feature.js": `export function describe(seed) { return \`feature:\${seed}\`; }\n`,
      "helpers.js": `export function addOne(value) { return value + 1; }\n`,
    },
    entryFile: "runtime.js",
    prefix: "debundle-extract-ordered-init-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("extracts an ordered-init region from a non-runtime entry file", () => {
  const source = `const seed = 1;
function render() {
  return seed + 1;
}
console.log(render());
export { render as publicRender };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    orderedInitOperation(source, {
      file: "entry.js",
      init: "init_entry_region",
      ownerNames: ["seed", "render"],
      targetFile: "regions/entry_region.js",
    }),
  ]);

  const entryCode = result.files.get("entry.js");
  const extractedCode = result.files.get("regions/entry_region.js");
  assert.match(entryCode, /from "\.\/regions\/entry_region\.js"/);
  assert.match(entryCode, /init_entry_region_stage_0\(\)/);
  assert.match(extractedCode, /export function init_entry_region/);
  assertRunnableEquivalent({
    entryFile: "entry.js",
    files: {},
    prefix: "debundle-extract-entry-file-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("rebases nested dynamic imports inside extracted stage bodies", () => {
  const source = `async function loadFeature() {
  const mod = await import("./feature.js");
  return mod.describe("ok");
}
console.log(await loadFeature());
export { loadFeature };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    orderedInitOperation(source, {
      init: "init_dynamic_import_region",
      ownerNames: ["loadFeature"],
      targetFile: "regions/deep/dynamic_import_region.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/deep/dynamic_import_region.js");
  assert.match(extractedCode, /await import\("\.\.\/\.\.\/feature\.js"\)/);

  assertRunnableEquivalent({
    files: {
      "feature.js": `export function describe(value) { return \`feature:\${value}\`; }\n`,
    },
    prefix: "debundle-extract-ordered-init-dynamic-import-rebase-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("rebases worker constructor literals inside extracted stage bodies", () => {
  const source = `function spawnWorker() {
  return new Worker("./workers/render_worker.js");
}
export { spawnWorker };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    orderedInitOperation(source, {
      init: "init_worker_rebase_region",
      ownerNames: ["spawnWorker"],
      targetFile: "regions/deep/worker_rebase_region.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/deep/worker_rebase_region.js");
  assert.match(
    extractedCode,
    /new Worker\(new URL\("\.\.\/\.\.\/workers\/render_worker\.js", import\.meta\.url\)\)/
  );
});

test("rebases shared worker constructor literals inside extracted stage bodies", () => {
  const source = `function spawnSharedWorker() {
  return new SharedWorker("./workers/render_worker.js");
}
export { spawnSharedWorker };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    orderedInitOperation(source, {
      init: "init_shared_worker_rebase_region",
      ownerNames: ["spawnSharedWorker"],
      targetFile: "regions/deep/shared_worker_rebase_region.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/deep/shared_worker_rebase_region.js");
  assert.match(
    extractedCode,
    /new SharedWorker\(new URL\("\.\.\/\.\.\/workers\/render_worker\.js", import\.meta\.url\)\)/
  );
});

test("keeps shadowed worker constructor literals unchanged inside extracted stage bodies", () => {
  const source = `function spawnWorker() {
  class Worker {
    constructor(url) {
      this.url = url;
    }
  }
  return new Worker("./workers/render_worker.js");
}
export { spawnWorker };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    orderedInitOperation(source, {
      init: "init_shadowed_worker_region",
      ownerNames: ["spawnWorker"],
      targetFile: "regions/deep/shadowed_worker_region.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/deep/shadowed_worker_region.js");
  assert.match(extractedCode, /new Worker\("\.\/workers\/render_worker\.js"\)/);
  assert.doesNotMatch(extractedCode, /new URL\(/);
});

test("keeps var-hoisted worker constructor literals unchanged inside extracted stage bodies", () => {
  const source = `function spawnWorker() {
  if (true) {
    var Worker = class WorkerImpl {
      constructor(url) {
        this.url = url;
      }
    };
  }
  return new Worker("./workers/render_worker.js");
}
export { spawnWorker };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    orderedInitOperation(source, {
      init: "init_var_shadowed_worker_region",
      ownerNames: ["spawnWorker"],
      targetFile: "regions/deep/var_shadowed_worker_region.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/deep/var_shadowed_worker_region.js");
  assert.match(extractedCode, /new Worker\("\.\/workers\/render_worker\.js"\)/);
  assert.doesNotMatch(extractedCode, /new URL\(/);
});

test("extracts non-contiguous declaration regions with staged-shell lowering by default", () => {
  const source = `const a = 1;
console.log("barrier");
const b = 2;
console.log(a + b);
`;
  const result = extractOrderedInitRegionsInCode(source, [
    orderedInitOperation(source, {
      init: "init_non_contiguous",
      ownerNames: ["a", "b"],
      targetFile: "regions/non_contiguous.js",
    }),
  ]);

  assert.match(result.files.get("runtime.js"), /init_non_contiguous_stage_0\(\)/);
  assert.ok(result.files.get("regions/non_contiguous.js"));
});

test("extracts non-contiguous declaration regions with staged-shell lowering and preserves behavior", () => {
  const source = `const helperSeed = 1;
function readHelperSeed() {
  return helperSeed;
}
console.log("stage-barrier");
const composed = readHelperSeed() + 1;
function render() {
  return composed;
}
console.log(JSON.stringify({ helper: readHelperSeed(), value: render() }));
export { render as publicRender };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    {
      ...orderedInitOperation(source, {
        init: "init_staged_fixture_region",
        ownerNames: ["helperSeed", "readHelperSeed", "composed", "render"],
        targetFile: "regions/staged_fixture_region.js",
      }),
      lowering: "staged_shell",
    },
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/staged_fixture_region.js");
  assert.match(runtimeCode, /init_staged_fixture_region_stage_0\(\);/);
  assert.match(runtimeCode, /init_staged_fixture_region_stage_1\(\);/);
  assert.match(extractedCode, /export function init_staged_fixture_region_stage_0/);
  assert.match(extractedCode, /export function init_staged_fixture_region_stage_1/);

  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-staged-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction keeps owner-to-statement mapping aligned across destructuring declarations", () => {
  const source = `const [left, right] = [1, 2];
const seed = 3;
function render() {
  return seed;
}
console.log(left + right, render());
export { render as publicRender };
`;
  const analysis = analyzeRuntimeBoundaryCode(source, {
    chunkId: "static/destructuring-alignment",
    runtimePath: "fixture/runtime.js",
    uiVersion: "fixture",
  });
  const ownerByName = new Map(
    analysis.owners.flatMap((owner) => owner.names.map((name) => [name, owner]))
  );

  const result = extractOrderedInitRegionsInCode(source, [
    {
      graphGenerated: true,
      id: "graph_destructuring_alignment",
      operation: "extract_ordered_init_region",
      selector: {
        chunkId: "static/destructuring-alignment",
        file: "runtime.js",
        ownerIds: [ownerByName.get("seed").id, ownerByName.get("render").id],
      },
      target: {
        file: "regions/destructuring_alignment.js",
        init: "init_destructuring_alignment",
      },
    },
  ], {
    chunkId: "static/destructuring-alignment",
    file: "runtime.js",
  });

  assert.match(
    result.files.get("runtime.js"),
    /from "\.\/regions\/destructuring_alignment\.js"/
  );
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-destructuring-alignment-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction snapshots self-referential variable declarations when bindings stay immutable", () => {
  const source = `var Status = ((Status2) => {
  Status2.Idle = "idle";
  Status2.Busy = "busy";
  return Status2;
})(Status || {});
function readStatus() {
  return Status.Busy;
}
console.log(readStatus());
export { readStatus };
`;
  const analysis = analyzeRuntimeBoundaryCode(source, {
    chunkId: "static/snapshot-variable-owner",
    runtimePath: "fixture/runtime.js",
    uiVersion: "fixture",
  });
  const ownerByName = new Map(
    analysis.owners.flatMap((owner) => owner.names.map((name) => [name, owner]))
  );
  assert.equal(ownerByName.get("Status").currentExtractorCompatible, true);
  assert.equal(ownerByName.get("Status").currentExtractorLowering, "snapshot_variable_declaration");

  const result = extractOrderedInitRegionsInCode(source, [
    {
      graphGenerated: true,
      id: "graph_snapshot_variable_owner",
      operation: "extract_ordered_init_region",
      selector: {
        chunkId: "static/snapshot-variable-owner",
        file: "runtime.js",
        ownerIds: [ownerByName.get("Status").id, ownerByName.get("readStatus").id],
      },
      target: {
        file: "regions/snapshot_variable_owner.js",
        init: "init_snapshot_variable_owner",
      },
    },
  ], {
    chunkId: "static/snapshot-variable-owner",
    file: "runtime.js",
  });

  const extractedCode = result.files.get("regions/snapshot_variable_owner.js");
  assert.match(extractedCode, /const __snapshot_owner_00000 = \(\(\) =>/);
  assert.match(extractedCode, /Status = __snapshot_owner_00000\.Status/);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-snapshot-variable-owner-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("extractOrderedInitRegionsInCode reuses caller-supplied boundary analysis", () => {
  const source = `const seed = 1;
function render() {
  return seed + 1;
}
console.log(render());
`;
  const analysis = analyzeRuntimeBoundaryCode(source, {
    chunkId: "static/app",
    runtimePath: "runtime.js",
    uiVersion: "fixture",
  });
  const brokenAnalysis = {
    ...analysis,
    owners: analysis.owners.filter((owner) => !owner.names.includes("render")),
  };

  assert.throws(
    () =>
      extractOrderedInitRegionsInCode(
        source,
        [
          orderedInitOperation(source, {
            init: "init_reused_analysis",
            ownerNames: ["seed", "render"],
            targetFile: "regions/reused_analysis.js",
          }),
        ],
        {
          analysis: brokenAnalysis,
        }
      ),
    /references unknown owner/
  );
});

test("rejects selected owners that depend on outside runtime bindings", () => {
  const source = `const shared = 2;
const extracted = shared + 1;
console.log(extracted);
`;
  assert.throws(
    () =>
      extractOrderedInitRegionsInCode(source, [
        orderedInitOperation(source, {
          init: "init_external_dependency",
          ownerNames: ["extracted"],
          targetFile: "regions/external_dependency.js",
        }),
      ]),
    /depends on local runtime owner/
  );
});

test("rejects remaining runtime code that writes extracted bindings", () => {
  const source = `let value = 1;
function read() {
  return value;
}
function bump() {
  value += 1;
  return value;
}
console.log(read(), bump());
`;
  assert.throws(
    () =>
      extractOrderedInitRegionsInCode(source, [
        orderedInitOperation(source, {
          init: "init_write_conflict",
          ownerNames: ["value", "read"],
          targetFile: "regions/write_conflict.js",
        }),
      ]),
    /writing extracted binding value/
  );
});

test("extracts a self-contained class-style singleton region", () => {
  const source = `import { now } from "./clock.js";

class DeferredRenderCounter {
  constructor() {
    this.startedAt = now();
    this.count = 0;
  }
  bump() {
    this.count += 1;
    return this.count;
  }
}
const counter = new DeferredRenderCounter();
const first = counter.bump();
console.log(first, counter.startedAt > 0);
export { DeferredRenderCounter, counter, first };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    orderedInitOperation(source, {
      init: "init_counter_region",
      ownerNames: ["DeferredRenderCounter", "counter", "first"],
      targetFile: "regions/counter_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/counter_region.js");
  assert.match(runtimeCode, /import \{ DeferredRenderCounter, counter, first, init_counter_region_stage_0 \} from "\.\/regions\/counter_region\.js"/);
  assert.match(extractedCode, /import \{ now \} from "\.\.\/clock\.js"/);
  assert.match(extractedCode, /DeferredRenderCounter = class DeferredRenderCounter/);

  assertRunnableEquivalent({
    files: {
      "clock.js": `export function now() { return 7; }\n`,
    },
    prefix: "debundle-extract-ordered-init-class-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("extracts schema-style regions with forward/self references via snapshot lowering", () => {
  const source = `import { buildLeaf, buildNode } from "./schema.js";

var leafSchema = buildLeaf(),
  branchSchema = buildNode(leafSchema, treeSchema),
  treeSchema = buildNode(branchSchema, leafSchema);

function parseTree(input) {
  return treeSchema.parse(input);
}
console.log(parseTree("ok"));
export { parseTree };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    orderedInitOperation(source, {
      init: "init_schema_region",
      ownerNames: ["leafSchema", "parseTree"],
      targetFile: "regions/schema_region.js",
    }),
  ]);

  assertRunnableEquivalent({
    files: {
      "schema.js": `export function buildLeaf() { return { parse(input) { return input; } }; }\nexport function buildNode(left, right) { return { left, right, parse(input) { return input; } }; }\n`,
    },
    prefix: "debundle-extract-ordered-init-schema-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("extracts schema-style regions with lazy cross-declarator references without snapshot lowering", () => {
  const source = `const leafSchema = {
  parse(input) {
    return \`leaf:\${input}\`;
  },
},
  renderTree = (input) => treeSchema.parse(leafSchema.parse(input)),
  treeSchema = {
    parse(input) {
      return \`tree:\${input}\`;
    },
  };

console.log(renderTree("ok"));
export { renderTree, treeSchema };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    orderedInitOperation(source, {
      init: "init_lazy_schema_region",
      ownerNames: ["leafSchema"],
      targetFile: "regions/lazy_schema_region.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/lazy_schema_region.js");
  assert.doesNotMatch(extractedCode, /__snapshot_/);
  assert.match(extractedCode, /renderTree = .*treeSchema\.parse\(leafSchema\.parse\(input\)\)/);

  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-lazy-schema-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("staged-shell extraction preserves runtime imports used by attached side effects", () => {
  const source = `import { record } from "./schema.js";

const schema = { type: "manual" };
record("name", [schema]);
console.log(JSON.stringify(schema));
export { schema };
`;
  const analysis = analyzeRuntimeBoundaryCode(source, { chunkId: "static/app" });
  const schemaOwner = analysis.owners.find((owner) => owner.names.includes("schema"));
  const attachedSideEffect = analysis.sideEffects.find((sideEffect) =>
    sideEffect.readsTopLevel.eager.some((access) => access.kind === "runtime_import" && access.name === "record")
  );
  assert.ok(schemaOwner);
  assert.ok(attachedSideEffect);

  const result = extractOrderedInitRegionsInCode(source, [
    {
      id: "extract_attached_runtime_import",
      operation: "extract_ordered_init_region",
      selector: {
        chunkId: "static/app",
        file: "runtime.js",
        ownerIds: [schemaOwner.id],
        attachedItemIds: [attachedSideEffect.id],
      },
      target: {
        file: "regions/attached_runtime_import.js",
        init: "init_attached_runtime_import",
      },
      lowering: "staged_shell",
    },
  ]);

  const extractedCode = result.files.get("regions/attached_runtime_import.js");
  assert.match(extractedCode, /import \{ record \} from "\.\.\/schema\.js"/);
  assert.match(extractedCode, /record\("name", \[schema\]\)/);

  assertRunnableEquivalent({
    files: {
      "schema.js": `export function record(name, list) { for (const item of list) item.recorded = name; }\n`,
    },
    prefix: "debundle-extract-ordered-init-attached-runtime-import-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("materializes only the runtime import specifiers each extracted entry uses", () => {
  const source = `import defaultValue, { alpha as importedAlpha, beta, gamma } from "./schema.js";

const left = defaultValue(importedAlpha);
const right = beta + gamma;
console.log(left, right);
export { left, right };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    orderedInitOperation(source, {
      init: "init_left_region",
      ownerNames: ["left"],
      targetFile: "regions/left_region.js",
    }),
    orderedInitOperation(source, {
      init: "init_right_region",
      ownerNames: ["right"],
      targetFile: "regions/right_region.js",
    }),
  ]);

  const leftModuleCode = result.files.get("regions/left_region.js");
  const rightModuleCode = result.files.get("regions/right_region.js");
  assert.match(
    leftModuleCode,
    /import defaultValue, \{ alpha as importedAlpha \} from "\.\.\/schema\.js"/
  );
  assert.doesNotMatch(leftModuleCode, /\bbeta\b/);
  assert.doesNotMatch(leftModuleCode, /\bgamma\b/);
  assert.match(rightModuleCode, /import \{ beta, gamma \} from "\.\.\/schema\.js"/);
  assert.doesNotMatch(rightModuleCode, /\bdefaultValue\b/);
  assert.doesNotMatch(rightModuleCode, /\bimportedAlpha\b/);

  assertRunnableEquivalent({
    files: {
      "schema.js": `export default function defaultValue(value) { return \`left:\${value}\`; }\nexport const alpha = "a";\nexport const beta = 2;\nexport const gamma = 3;\n`,
    },
    prefix: "debundle-extract-ordered-init-runtime-import-specifiers-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("extracts a local state cluster when the dependency closure stays inside the region", () => {
  const source = `let open = false;
function toggle() {
  open = !open;
  return open;
}
function isOpen() {
  return open;
}
const initial = isOpen();
const next = toggle();
console.log(initial, next, isOpen());
export { isOpen, next, toggle };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    orderedInitOperation(source, {
      init: "init_state_region",
      ownerNames: ["open", "toggle", "isOpen", "initial", "next"],
      targetFile: "regions/state_region.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/state_region.js");
  assert.match(extractedCode, /export let open, toggle, isOpen, initial, next;/);
  assert.match(extractedCode, /open = false/);
  assert.match(extractedCode, /toggle = function toggle/);

  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-state-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("rejects a drag-selection style region with a lazy read from a helper left outside", () => {
  const source = `const dragSelectionState = { origin: 2 };
function readDragSelectionOrigin() {
  return dragSelectionState.origin;
}
class DragSelectionSession {
  current() {
    return readDragSelectionOrigin();
  }
}
const dragSelectionSession = new DragSelectionSession();
console.log(dragSelectionSession.current());
`;
  assert.throws(
    () =>
      extractOrderedInitRegionsInCode(source, [
        orderedInitOperation(source, {
          init: "init_drag_selection_region",
          ownerNames: ["DragSelectionSession", "dragSelectionSession"],
          targetFile: "regions/drag_selection_region.js",
        }),
      ]),
    /depends on local runtime owner .* via lazy read/
  );
});

test("rejects extraction when earlier eager code would observe the moved binding too soon", () => {
  const source = `console.log(InvalidNodeIdError.name);
class InvalidNodeIdError extends Error {}
throw new InvalidNodeIdError("bad id");
`;
  assert.throws(
    () =>
      extractOrderedInitRegionsInCode(source, [
        orderedInitOperation(source, {
          init: "init_error_region",
          ownerNames: ["InvalidNodeIdError"],
          targetFile: "regions/error_region.js",
        }),
      ]),
    /would move binding InvalidNodeIdError after eager use/
  );
});

test("rejects staged-shell lowering when a retained shell statement eagerly uses a later owner", () => {
  const source = `const helperSeed = 1;
console.log(readLater());
function readLater() {
  return helperSeed;
}
const rendered = readLater() + 1;
console.log(rendered);
`;
  assert.throws(
    () =>
      extractOrderedInitRegionsInCode(source, [
        {
          ...orderedInitOperation(source, {
            init: "init_staged_later_use_region",
            ownerNames: ["helperSeed", "readLater", "rendered"],
            targetFile: "regions/staged_later_use_region.js",
          }),
          lowering: "staged_shell",
        },
      ]),
    /staged shell item .* eagerly uses later extracted owner/
  );
});

function orderedInitOperation(source, { file = "runtime.js", init, ownerNames, targetFile }) {
  const ownerIds = ownerIdsForNames(source, ownerNames);
  return {
    id: `extract_${init}`,
    operation: "extract_ordered_init_region",
    selector: {
      chunkId: "static/app",
      file,
      ownerIds,
    },
    target: {
      file: targetFile,
      init,
    },
  };
}

function ownerIdsForNames(source, names) {
  const analysis = analyzeRuntimeBoundaryCode(source, { chunkId: "static/app" });
  return names.map((name) => {
    const owner = analysis.owners.find((candidate) => candidate.names.includes(name));
    if (!owner) {
      throw new Error(`Fixture owner not found for ${name}`);
    }
    return owner.id;
  });
}

function assertRunnableEquivalent({ entryFile = "runtime.js", files, prefix, source, transformedFiles }) {
  const originalDir = createTempFixtureRoot(`${prefix}original-`);
  const transformedDir = createTempFixtureRoot(`${prefix}transformed-`);
  writeRunnableFixture(originalDir, {
    files: {
      ...files,
      [entryFile]: source,
    },
  });
  writeRunnableFixture(transformedDir, {
    files: {
      ...files,
      ...transformedFiles,
    },
  });
  assert.deepEqual(runNodeScript(join(transformedDir, entryFile)), runNodeScript(join(originalDir, entryFile)));
}
