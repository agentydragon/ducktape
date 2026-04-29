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
    selectedModuleOperation(source, {
      init: "init_fixture_region",
      ownerNames: ["seed", "format", "Box", "loadFeature", "result"],
      targetFile: "regions/fixture_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/fixture_region.js");
  assert.match(
    extractedCode,
    /^\/\/ @ducktape-generated kind=lowerer-helper stage=selected_module_lowering ignore=detectors\n\/\/ @ducktape-generator devinfra\/js\/debundle\/extract\/init_region\.mjs\n\/\/ Selected-module lowered region; original owners:/,
  );
  assert.match(runtimeCode, /import \{ seed, format, Box, loadFeature, result, init_fixture_region \} from "\.\/regions\/fixture_region\.js"/);
  assert.match(runtimeCode, /@ducktape-generated-node kind=lowerer-glue stage=selected_module_lowering/);
  assert.match(runtimeCode, /init_fixture_region\(\);/);
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
    selectedModuleOperation(source, {
      file: "entry.js",
      init: "init_entry_region",
      ownerNames: ["seed", "render"],
      targetFile: "regions/entry_region.js",
    }),
  ]);

  const entryCode = result.files.get("entry.js");
  const extractedCode = result.files.get("regions/entry_region.js");
  assert.match(entryCode, /from "\.\/regions\/entry_region\.js"/);
  assert.doesNotMatch(entryCode, /init_entry_region\(\)/);
  assert.match(extractedCode, /\bconst seed = 1;/);
  assert.match(extractedCode, /\bfunction render\(\)/);
  assert.doesNotMatch(extractedCode, /export function init_entry_region/);
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
    selectedModuleOperation(source, {
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
    selectedModuleOperation(source, {
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
    selectedModuleOperation(source, {
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
    selectedModuleOperation(source, {
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
    selectedModuleOperation(source, {
      init: "init_var_shadowed_worker_region",
      ownerNames: ["spawnWorker"],
      targetFile: "regions/deep/var_shadowed_worker_region.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/deep/var_shadowed_worker_region.js");
  assert.match(extractedCode, /new Worker\("\.\/workers\/render_worker\.js"\)/);
  assert.doesNotMatch(extractedCode, /new URL\(/);
});

test("extracts non-contiguous pure declaration regions as plain imports by default", () => {
  const source = `const a = 1;
console.log("barrier");
const b = 2;
console.log(a + b);
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_non_contiguous",
      ownerNames: ["a", "b"],
      targetFile: "regions/non_contiguous.js",
    }),
  ]);

  assert.match(result.files.get("runtime.js"), /import \{ a, b \} from "\.\/regions\/non_contiguous\.js"/);
  assert.doesNotMatch(result.files.get("runtime.js"), /\binit_non_contiguous\b/);
  assert.match(result.files.get("regions/non_contiguous.js"), /\bconst a = 1;/);
  assert.match(result.files.get("regions/non_contiguous.js"), /\bconst b = 2;/);
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
      ...selectedModuleOperation(source, {
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
      operation: "lower_selected_module_region",
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

test("graph-generated extraction lowers safe destructuring declarations directly", () => {
  const source = `globalThis.sharedCodec = {
  decode(value) {
    return value.toUpperCase();
  },
};
const { decode: decodeUpper } = globalThis.sharedCodec;
function readDecodedValue() {
  return decodeUpper("ok");
}
console.log(readDecodedValue());
export { readDecodedValue };
`;
  const analysis = analyzeRuntimeBoundaryCode(source, {
    chunkId: "static/destructuring-standard",
    runtimePath: "fixture/runtime.js",
    uiVersion: "fixture",
  });
  const ownerByName = new Map(
    analysis.owners.flatMap((owner) => owner.names.map((name) => [name, owner]))
  );

  assert.equal(ownerByName.get("decodeUpper").currentExtractorCompatible, true);
  assert.equal(ownerByName.get("decodeUpper").currentExtractorLowering, "standard");

  const result = extractOrderedInitRegionsInCode(source, [
    {
      graphGenerated: true,
      id: "graph_destructuring_standard",
      operation: "lower_selected_module_region",
      selector: {
        chunkId: "static/destructuring-standard",
        file: "runtime.js",
        ownerIds: [ownerByName.get("decodeUpper").id, ownerByName.get("readDecodedValue").id],
      },
      target: {
        file: "regions/destructuring_standard.js",
        init: "init_destructuring_standard",
      },
    },
  ], {
    chunkId: "static/destructuring-standard",
    file: "runtime.js",
  });

  const extractedCode = result.files.get("regions/destructuring_standard.js");
  assert.match(extractedCode, /\(\{\s*decode: decodeUpper\s*\} = globalThis\.sharedCodec\);/s);
  assert.doesNotMatch(extractedCode, /__dt_selected_module_snapshot__/);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-destructuring-standard-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction keeps destructured object param aliases when a readable rename would shadow an outer binding", () => {
  const source = `const resourceIds = "outer";
function c7t({ resourceIds: n, startDateTime: e }) {
  return resourceIds + ":" + n + ":" + e;
}
console.log(c7t({
  resourceIds: "inner",
  startDateTime: "start",
}));
export { c7t };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_param_shadow_guard",
      ownerNames: ["resourceIds", "c7t"],
      targetFile: "regions/param_shadow_guard.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/param_shadow_guard.js");
  assert.match(extractedCode, /resourceIds: n,\s*startDateTime/s);
  assert.match(extractedCode, /return resourceIds \+ ":" \+ n \+ ":" \+ startDateTime;/);
  assert.doesNotMatch(extractedCode, /\{ resourceIds, startDateTime \}/);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-param-shadow-guard-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction renames destructured function-expression params when object shorthand reuses the same bindings", () => {
  const source = `const _Wt = async function _Wt({ nodeSpace: n, text: e, tagsAsPromptSection: t }) {
  return {
    nodeSpace: n,
    text: e,
    tagsAsPromptSection: t,
  };
};
console.log(JSON.stringify(await _Wt({
  nodeSpace: "space",
  text: "hello",
  tagsAsPromptSection: true,
})));
export { _Wt };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_function_expression_param_readable_names",
      ownerNames: ["_Wt"],
      targetFile: "regions/function_expression_param_readable_names.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/function_expression_param_readable_names.js");
  assert.match(
    extractedCode,
    /async function _Wt\(\{\s*nodeSpace,\s*text,\s*tagsAsPromptSection\s*\}\)/s
  );
  assert.match(extractedCode, /return \{\s*nodeSpace,\s*text,\s*tagsAsPromptSection\s*\};/s);
  assert.doesNotMatch(extractedCode, /\{\s*nodeSpace: n,\s*text: e,\s*tagsAsPromptSection: t\s*\}/s);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-function-expression-param-readable-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction keeps function-expression param aliases when a readable rename would shadow an outer binding", () => {
  const source = `const nodeSpace = "outer";
const _Wt = async function _Wt({ nodeSpace: n, text: e }) {
  return nodeSpace + ":" + n + ":" + e;
};
console.log(await _Wt({
  nodeSpace: "inner",
  text: "hello",
}));
export { _Wt };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_function_expression_param_shadow_guard",
      ownerNames: ["nodeSpace", "_Wt"],
      targetFile: "regions/function_expression_param_shadow_guard.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/function_expression_param_shadow_guard.js");
  assert.match(extractedCode, /nodeSpace: n,\s*text/s);
  assert.match(extractedCode, /return nodeSpace \+ ":" \+ n \+ ":" \+ text;/);
  assert.doesNotMatch(extractedCode, /\{\s*nodeSpace,\s*text\s*\}/s);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-function-expression-param-shadow-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction renames constructor params from this-property assignments", () => {
  const source = `class AsyncAvatarAccessor {
  constructor(e, t) {
    this.asyncNode = e, this.core = t;
  }
  label() {
    return this.asyncNode + ":" + this.core;
  }
}
console.log(new AsyncAvatarAccessor("node", "core").label());
export { AsyncAvatarAccessor };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_constructor_param_readable_names",
      ownerNames: ["AsyncAvatarAccessor"],
      targetFile: "regions/constructor_param_readable_names.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/constructor_param_readable_names.js");
  assert.match(extractedCode, /constructor\(asyncNode, core\)/);
  assert.match(extractedCode, /this\.asyncNode = asyncNode[,;]/);
  assert.match(extractedCode, /this\.core = core;/);
  assert.doesNotMatch(extractedCode, /constructor\(e, t\)/);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-constructor-param-readable-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction keeps constructor params when a readable name collides locally", () => {
  const source = `class AsyncAvatarAccessor {
  constructor(e) {
    const asyncNode = "local";
    this.asyncNode = e;
    this.label = asyncNode + ":" + e;
  }
}
console.log(new AsyncAvatarAccessor("node").label);
export { AsyncAvatarAccessor };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_constructor_param_collision_guard",
      ownerNames: ["AsyncAvatarAccessor"],
      targetFile: "regions/constructor_param_collision_guard.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/constructor_param_collision_guard.js");
  assert.match(extractedCode, /constructor\(e\)/);
  assert.match(extractedCode, /const asyncNode = "local";/);
  assert.match(extractedCode, /this\.asyncNode = e;/);
  assert.doesNotMatch(extractedCode, /constructor\(asyncNode\)/);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-constructor-param-collision-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction keeps constructor params when a nested scope would shadow the readable name", () => {
  const source = `class AsyncAvatarAccessor {
  constructor(e) {
    this.asyncNode = e;
    this.read = () => {
      const asyncNode = "nested";
      return asyncNode + ":" + e;
    };
  }
}
console.log(new AsyncAvatarAccessor("node").read());
export { AsyncAvatarAccessor };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_constructor_param_shadow_guard",
      ownerNames: ["AsyncAvatarAccessor"],
      targetFile: "regions/constructor_param_shadow_guard.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/constructor_param_shadow_guard.js");
  assert.match(extractedCode, /constructor\(e\)/);
  assert.match(extractedCode, /this\.asyncNode = e;/);
  assert.match(extractedCode, /return asyncNode \+ ":" \+ e;/);
  assert.doesNotMatch(extractedCode, /constructor\(asyncNode\)/);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-constructor-param-shadow-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction keeps constructor params when assignments imply duplicate readable names", () => {
  const source = `class DuplicateValueBox {
  constructor(e, t) {
    this.value = e;
    this.value = t;
  }
}
console.log(new DuplicateValueBox("first", "second").value);
export { DuplicateValueBox };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_constructor_param_duplicate_guard",
      ownerNames: ["DuplicateValueBox"],
      targetFile: "regions/constructor_param_duplicate_guard.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/constructor_param_duplicate_guard.js");
  assert.match(extractedCode, /constructor\(e, t\)/);
  assert.match(extractedCode, /this\.value = e;/);
  assert.match(extractedCode, /this\.value = t;/);
  assert.doesNotMatch(extractedCode, /constructor\(value, value\)/);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-constructor-param-duplicate-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction renames simple object destructuring assignments to readable locals", () => {
  const source = `function r7t(n) {
  let e, t;
  ({ hostNode: e, hostParent: t } = n);
  return [e.id, t.id].join(":");
}
console.log(r7t({
  hostNode: { id: "node" },
  hostParent: { id: "parent" },
}));
export { r7t };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_object_assignment_readable_names",
      ownerNames: ["r7t"],
      targetFile: "regions/object_assignment_readable_names.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/object_assignment_readable_names.js");
  assert.match(extractedCode, /\blet hostNode, hostParent;/);
  assert.match(extractedCode, /\(\{\s*hostNode,\s*hostParent\s*\} = n\);/s);
  assert.match(extractedCode, /return \[hostNode\.id, hostParent\.id\]\.join\(":"\);/);
  assert.doesNotMatch(extractedCode, /hostNode: e/);
  assert.doesNotMatch(extractedCode, /hostParent: t/);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-object-assignment-readable-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction keeps destructuring assignment aliases when a readable rename would be shadowed in a nested scope", () => {
  const source = `function s7t(n) {
  let e;
  function readShadow() {
    const hostNode = "shadow";
    return e + ":" + hostNode;
  }
  ({ hostNode: e } = n);
  return readShadow();
}
console.log(s7t({
  hostNode: "outer",
}));
export { s7t };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_object_assignment_shadow_guard",
      ownerNames: ["s7t"],
      targetFile: "regions/object_assignment_shadow_guard.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/object_assignment_shadow_guard.js");
  assert.match(extractedCode, /\(\{\s*hostNode: e\s*\} = n\);/s);
  assert.match(extractedCode, /return e \+ ":" \+ hostNode;/);
  assert.doesNotMatch(extractedCode, /\(\{\s*hostNode\s*\} = n\);/s);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-object-assignment-shadow-guard-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction renames return-object aliases to readable shorthand locals", () => {
  const source = `function r8t(n) {
  const includeTitle = n.includeTitle;
  const l = n.includeDescription;
  return {
    includeTitle: includeTitle,
    includeDescription: l,
  };
}
console.log(JSON.stringify(r8t({
  includeTitle: true,
  includeDescription: false,
})));
export { r8t };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_return_object_readable_names",
      ownerNames: ["r8t"],
      targetFile: "regions/return_object_readable_names.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/return_object_readable_names.js");
  assert.match(extractedCode, /\bconst includeTitle = n\.includeTitle;/);
  assert.match(extractedCode, /\bconst includeDescription = n\.includeDescription;/);
  assert.match(extractedCode, /return \{\s*includeTitle,\s*includeDescription\s*\};/s);
  assert.doesNotMatch(extractedCode, /includeTitle: includeTitle/);
  assert.doesNotMatch(extractedCode, /includeDescription: l/);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-return-object-readable-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction keeps return-object aliases when a readable rename would collide with an existing local binding", () => {
  const source = `function s8t(n) {
  const includeTitle = n.currentTitle;
  const i = n.overrideTitle;
  return {
    includeTitle: i,
    fallbackTitle: includeTitle,
  };
}
console.log(JSON.stringify(s8t({
  currentTitle: "current",
  overrideTitle: "override",
})));
export { s8t };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_return_object_collision_guard",
      ownerNames: ["s8t"],
      targetFile: "regions/return_object_collision_guard.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/return_object_collision_guard.js");
  assert.match(extractedCode, /return \{\s*includeTitle: i,\s*fallbackTitle: includeTitle\s*\};/s);
  assert.doesNotMatch(extractedCode, /return \{\s*includeTitle,\s*fallbackTitle: includeTitle\s*\};/s);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-return-object-collision-guard-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction lowers self-contained snapshot variable declarations directly", () => {
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
      operation: "lower_selected_module_region",
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
  const runtimeCode = result.files.get("runtime.js");
  assert.match(runtimeCode, /import \{ Status, readStatus \} from "\.\/regions\/snapshot_variable_owner\.js"/);
  assert.doesNotMatch(runtimeCode, /\binit_snapshot_variable_owner\b/);
  assert.match(extractedCode, /\bvar Status = \(Status2 =>/);
  assert.match(extractedCode, /\bfunction readStatus\(\)/);
  assert.doesNotMatch(extractedCode, /__dt_selected_module_snapshot__/);
  assert.doesNotMatch(extractedCode, /export function init_snapshot_variable_owner/);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-snapshot-variable-owner-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction naturalizes return-object aliases while preserving runtime export aliases", () => {
  const source = `function r8t(n) {
  const includeTitle = n.includeTitle;
  const l = n.includeDescription;
  return {
    includeTitle: includeTitle,
    includeDescription: l,
  };
}
console.log(JSON.stringify(r8t({
  includeTitle: true,
  includeDescription: false,
})));
export { r8t as publicBuildPoint };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_point_region",
      ownerNames: ["r8t"],
      targetFile: "regions/point_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/point_region.js");
  assert.match(runtimeCode, /import \{ r8t \} from "\.\/regions\/point_region\.js"/);
  assert.match(runtimeCode, /export \{ r8t as publicBuildPoint \};/);
  assert.match(extractedCode, /\bconst includeTitle = n\.includeTitle;/);
  assert.match(extractedCode, /\bconst includeDescription = n\.includeDescription;/);
  assert.match(extractedCode, /\bfunction r8t\(n\)/);
  assert.match(extractedCode, /return \{\s*includeTitle,\s*includeDescription\s*\};/s);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-readable-export-rename-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated readable renames preserve same-spelled nested bindings", () => {
  const source = `function r8t(n) {
  const l = n.label;
  function nested() {
    const l = "shadow";
    return l;
  }
  return {
    label: l,
    nested,
  };
}
const result = r8t({ label: "outer" });
console.log(result.label, result.nested());
export { r8t };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_shadowed_readable_region",
      ownerNames: ["r8t"],
      targetFile: "regions/shadowed_readable_region.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/shadowed_readable_region.js");
  assert.match(extractedCode, /\bconst label = n\.label;/);
  assert.match(extractedCode, /\bconst l = "shadow";/);
  assert.match(extractedCode, /return \{\s*label,\s*nested\s*\};/s);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-readable-shadowed-binding-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("lowers multi-stage pure declaration regions as plain imports without init wrappers", () => {
  const source = `const alpha = 1;
const gapLabel = "gap";
function readAlpha() {
  return alpha;
}
const beta = 2;
function readBeta() {
  return beta;
}
console.log(readAlpha(), gapLabel, readBeta());
export { gapLabel };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_plain_multi_stage_region",
      ownerNames: ["alpha", "readAlpha", "beta", "readBeta"],
      targetFile: "regions/plain_multi_stage_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/plain_multi_stage_region.js");
  assert.match(runtimeCode, /import \{ alpha, readAlpha, beta, readBeta \} from "\.\/regions\/plain_multi_stage_region\.js"/);
  assert.doesNotMatch(runtimeCode, /\binit_plain_multi_stage_region\b/);
  assert.match(runtimeCode, /\bconst gapLabel = "gap";/);
  assert.match(extractedCode, /\bconst alpha = 1;/);
  assert.match(extractedCode, /\bfunction readAlpha\(\)/);
  assert.match(extractedCode, /\bconst beta = 2;/);
  assert.match(extractedCode, /\bfunction readBeta\(\)/);
  assert.doesNotMatch(extractedCode, /export function init_plain_multi_stage_region/);
  assert.doesNotMatch(extractedCode, /\n\s*alpha = 1;/);
  assert.doesNotMatch(extractedCode, /\n\s*beta = 2;/);

  assertRunnableEquivalent({
    prefix: "debundle-extract-plain-import-multi-stage-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("graph-generated extraction snapshots destructuring declarations with forward references when bindings stay immutable", () => {
  const source = `const { chosen = fallbackLabel } = {}, fallbackLabel = "fallback";
function readChosen() {
  return chosen;
}
console.log(readChosen());
export { readChosen };
`;
  const analysis = analyzeRuntimeBoundaryCode(source, {
    chunkId: "static/snapshot-destructuring-owner",
    runtimePath: "fixture/runtime.js",
    uiVersion: "fixture",
  });
  const ownerByName = new Map(
    analysis.owners.flatMap((owner) => owner.names.map((name) => [name, owner]))
  );
  assert.equal(ownerByName.get("chosen").currentExtractorCompatible, true);
  assert.equal(ownerByName.get("chosen").currentExtractorLowering, "snapshot_variable_declaration");

  const result = extractOrderedInitRegionsInCode(source, [
    {
      graphGenerated: true,
      id: "graph_snapshot_destructuring_owner",
      operation: "lower_selected_module_region",
      selector: {
        chunkId: "static/snapshot-destructuring-owner",
        file: "runtime.js",
        ownerIds: [ownerByName.get("chosen").id, ownerByName.get("readChosen").id],
      },
      target: {
        file: "regions/snapshot_destructuring_owner.js",
        init: "init_snapshot_destructuring_owner",
      },
    },
  ], {
    chunkId: "static/snapshot-destructuring-owner",
    file: "runtime.js",
  });

  const extractedCode = result.files.get("regions/snapshot_destructuring_owner.js");
  assert.match(extractedCode, /const \{\s*chosen = fallbackLabel\s*\} = \{\},\s*fallbackLabel = "fallback";/s);
  assert.match(extractedCode, /\bfunction readChosen\(\)/);
  assert.doesNotMatch(extractedCode, /__dt_selected_module_snapshot__/);
  const originalDir = createTempFixtureRoot("debundle-extract-ordered-init-snapshot-destructuring-owner-original-");
  const transformedDir = createTempFixtureRoot("debundle-extract-ordered-init-snapshot-destructuring-owner-transformed-");
  writeRunnableFixture(originalDir, {
    files: {
      "runtime.js": source,
    },
  });
  writeRunnableFixture(transformedDir, {
    files: Object.fromEntries(result.files),
  });
  const originalRun = runNodeScript(join(originalDir, "runtime.js"));
  const transformedRun = runNodeScript(join(transformedDir, "runtime.js"));
  assert.equal(originalRun.status, 1);
  assert.equal(transformedRun.status, 1);
  assert.match(originalRun.stderr, /ReferenceError: Cannot access 'fallbackLabel' before initialization/);
  assert.match(transformedRun.stderr, /ReferenceError: Cannot access 'fallbackLabel' before initialization/);
  assert.equal(originalRun.stdout, transformedRun.stdout);
});

test("lowers single-stage pure variable/function regions as plain imports without init wrappers", () => {
  const source = `const OVt = 500;
function readTimeout() {
  return OVt;
}
const result = readTimeout();
console.log(result);
export { result };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_plain_timeout_region",
      ownerNames: ["OVt", "readTimeout"],
      targetFile: "regions/plain_timeout_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/plain_timeout_region.js");
  assert.match(runtimeCode, /import \{ OVt, readTimeout \} from "\.\/regions\/plain_timeout_region\.js"/);
  assert.doesNotMatch(runtimeCode, /\binit_plain_timeout_region\b/);
  assert.match(extractedCode, /\bconst OVt = 500;/);
  assert.match(extractedCode, /\bfunction readTimeout\(\)/);
  assert.doesNotMatch(extractedCode, /export function init_plain_timeout_region/);
  assert.doesNotMatch(extractedCode, /\n\s*OVt = 500;/);

  assertRunnableEquivalent({
    prefix: "debundle-extract-plain-import-timeout-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("alias-lowering elides init wrappers for direct alias reads", () => {
  const source = `import { importedSymbol } from "./dep.js";
const a = importedSymbol;
console.log(JSON.stringify({ a }));
export { a };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_alias_lowering_elide",
      ownerNames: ["a"],
      targetFile: "regions/alias_lowering_elide.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const elidedCode = result.files.get("regions/alias_lowering_elide.js");

  assert.match(runtimeCode, /import \{ a \} from "\.\/regions\/alias_lowering_elide\.js"/);
  assert.doesNotMatch(runtimeCode, /\binit_alias_lowering_elide\(\);/);
  assert.match(elidedCode, /\bconst a = importedSymbol;/);
  assert.doesNotMatch(elidedCode, /export function init_alias_lowering_elide/);

  assertRunnableEquivalent({
    files: {
      "dep.js": `export const importedSymbol = "imported-value";\n`,
    },
    prefix: "debundle-extract-alias-lowering-elide-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("alias-lowering keeps init wrappers for non-elidable alias declarators", () => {
  const source = `import { importedSymbol, ns, deep, obj, key, callFactory, Ctor } from "./dep.js";
const memberAlias = ns.member;
const deepAlias = deep.inner.leaf;
const computed = obj[key];
const fromCall = callFactory().member;
const fromNew = new Ctor().member;
const { picked } = obj;
const multiOne = 1, multiTwo = importedSymbol;
console.log(JSON.stringify({ memberAlias, deepAlias, computed, fromCall, fromNew, picked, multiOne, multiTwo }));
export { memberAlias, deepAlias, computed, fromCall, fromNew, picked, multiOne, multiTwo };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_alias_lowering_keep_computed",
      ownerNames: ["memberAlias", "deepAlias", "computed", "fromCall", "fromNew", "picked", "multiOne"],
      targetFile: "regions/alias_lowering_keep_cases.js",
    }),
  ]);
  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/alias_lowering_keep_cases.js");
  assert.match(runtimeCode, /\binit_alias_lowering_keep_computed\(\);/);
  assert.match(extractedCode, /computed = obj\[key\];/);
  assert.match(extractedCode, /fromCall = callFactory\(\)\.member;/);
  assert.match(extractedCode, /fromNew = new Ctor\(\)\.member;/);
  assert.match(extractedCode, /\(\{\s*picked\s*\} = obj\);/);
  assert.match(extractedCode, /multiOne = 1,\s*multiTwo = importedSymbol;/s);
  assert.match(extractedCode, /memberAlias = ns\.member;/);
  assert.match(extractedCode, /deepAlias = deep\.inner\.leaf;/);

  assertRunnableEquivalent({
    files: {
      "dep.js": `export const importedSymbol = "imported-value";
export const key = "member";
export const obj = { member: "computed-value" };
export const callFactory = () => ({ member: "call-value" });
export class Ctor {
  constructor() {
    this.member = "new-value";
  }
}
`,
    },
    prefix: "debundle-extract-alias-lowering-keep-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("lower_selected_module_region can split sibling fragments despite lazy member writes between them", () => {
  const source = `const KU = {}, ype = function ype() {
  KU.value = 1;
};
function render() {
  ype();
  return KU.value;
}
console.log(render());
export { render };
`;
  const [ownerId] = ownerIdsForNames(source, ["KU"]);
  const kuOperation = selectedModuleOperation(source, {
    init: "init_fragment_lazy_member_write_ku",
    ownerNames: ["KU"],
    targetFile: "regions/fragment_lazy_member_write_ku.js",
  });
  kuOperation.selector.ownerFragments = [
    {
      declaratorIndices: [0],
      id: `${ownerId}::declarator_0`,
      memberNames: ["KU"],
      orderIndex: 0,
      ownerId,
    },
  ];
  const ypeOperation = selectedModuleOperation(source, {
    init: "init_fragment_lazy_member_write_ype",
    ownerNames: ["ype"],
    targetFile: "regions/fragment_lazy_member_write_ype.js",
  });
  ypeOperation.selector.ownerFragments = [
    {
      declaratorIndices: [1],
      id: `${ownerId}::declarator_1`,
      memberNames: ["ype"],
      orderIndex: 1,
      ownerId,
    },
  ];

  const result = extractOrderedInitRegionsInCode(source, [kuOperation, ypeOperation]);

  const runtimeCode = result.files.get("runtime.js");
  const kuCode = result.files.get("regions/fragment_lazy_member_write_ku.js");
  const ypeCode = result.files.get("regions/fragment_lazy_member_write_ype.js");
  assert.match(runtimeCode, /regions\/fragment_lazy_member_write_ku\.js/);
  assert.match(runtimeCode, /regions\/fragment_lazy_member_write_ype\.js/);
  assert.match(kuCode, /\bKU = \{\};/);
  assert.doesNotMatch(kuCode, /\bype = function ype\(\)/);
  assert.match(ypeCode, /\bype = function ype\(\)/);
  assert.match(ypeCode, /import \{ KU \} from "\.\/fragment_lazy_member_write_ku\.js"/);

  assertRunnableEquivalent({
    prefix: "debundle-extract-fragment-lazy-member-write-",
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
          selectedModuleOperation(source, {
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
        selectedModuleOperation(source, {
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
        selectedModuleOperation(source, {
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
    selectedModuleOperation(source, {
      init: "init_counter_region",
      ownerNames: ["DeferredRenderCounter", "counter", "first"],
      targetFile: "regions/counter_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/counter_region.js");
  assert.match(runtimeCode, /import \{ DeferredRenderCounter, counter, first, init_counter_region \} from "\.\/regions\/counter_region\.js"/);
  assert.match(extractedCode, /import \{ now \} from "\.\.\/clock\.js"/);
  assert.match(extractedCode, /^\s*class DeferredRenderCounter\b/m);
  assert.match(extractedCode, /\blet counter, first\b/);
  assert.doesNotMatch(extractedCode, /\blet DeferredRenderCounter\b/);
  assert.doesNotMatch(extractedCode, /DeferredRenderCounter = class DeferredRenderCounter/);

  assertRunnableEquivalent({
    files: {
      "clock.js": `export function now() { return 7; }\n`,
    },
    prefix: "debundle-extract-ordered-init-class-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("lowers single-stage pure class/function regions as plain imports without init wrappers", () => {
  const source = `class PureBox {
  static label() {
    return "pure-box";
  }
}
function renderPureBox() {
  return PureBox.label();
}
const result = renderPureBox();
console.log(result);
export { result };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_plain_pure_region",
      ownerNames: ["PureBox", "renderPureBox"],
      targetFile: "regions/plain_pure_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/plain_pure_region.js");
  assert.match(runtimeCode, /import \{ PureBox, renderPureBox \} from "\.\/regions\/plain_pure_region\.js"/);
  assert.doesNotMatch(runtimeCode, /\binit_plain_pure_region\b/);
  assert.match(extractedCode, /\bclass PureBox\b/);
  assert.match(extractedCode, /\bfunction renderPureBox\b/);
  assert.doesNotMatch(extractedCode, /export function init_plain_pure_region/);
  assert.doesNotMatch(extractedCode, /\bPureBox = class PureBox\b/);

  assertRunnableEquivalent({
    prefix: "debundle-extract-plain-import-class-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("lowers class declarations with safe selected superclasses as plain imports", () => {
  const source = `class BaseBox {}
class DerivedBox extends BaseBox {
  static label() {
    return "derived";
  }
}
function renderDerivedBox() {
  return DerivedBox.label();
}
const result = renderDerivedBox();
console.log(result);
export { result };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_derived_box_region",
      ownerNames: ["BaseBox", "DerivedBox", "renderDerivedBox"],
      targetFile: "regions/derived_box_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/derived_box_region.js");
  assert.match(runtimeCode, /import \{ BaseBox, DerivedBox, renderDerivedBox \} from "\.\/regions\/derived_box_region\.js"/);
  assert.doesNotMatch(runtimeCode, /\binit_derived_box_region\b/);
  assert.match(extractedCode, /\bclass DerivedBox extends BaseBox\b/);
  assert.doesNotMatch(extractedCode, /export function init_derived_box_region/);
  assert.doesNotMatch(extractedCode, /\bDerivedBox = class DerivedBox extends BaseBox\b/);

  assertRunnableEquivalent({
    prefix: "debundle-extract-plain-import-selected-superclass-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("lowers class declarations with imported superclasses as plain imports", () => {
  const source = `import { BaseBox } from "./base_box.js";

class DerivedBox extends BaseBox {
  label() {
    return this.prefix + ":derived";
  }
}
function renderDerivedBox() {
  return new DerivedBox("safe").label();
}
const result = renderDerivedBox();
console.log(result);
export { result };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_imported_derived_box_region",
      ownerNames: ["DerivedBox", "renderDerivedBox"],
      targetFile: "regions/imported_derived_box_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/imported_derived_box_region.js");
  assert.match(runtimeCode, /import \{ DerivedBox, renderDerivedBox \} from "\.\/regions\/imported_derived_box_region\.js"/);
  assert.doesNotMatch(runtimeCode, /\binit_imported_derived_box_region\b/);
  assert.match(extractedCode, /import \{ BaseBox \} from "\.\.\/base_box\.js"/);
  assert.match(extractedCode, /\bclass DerivedBox extends BaseBox\b/);
  assert.doesNotMatch(extractedCode, /export function init_imported_derived_box_region/);
  assert.doesNotMatch(extractedCode, /\bDerivedBox = class DerivedBox extends BaseBox\b/);

  assertRunnableEquivalent({
    files: {
      "base_box.js": `export class BaseBox {
  constructor(prefix) {
    this.prefix = prefix;
  }
}
`,
    },
    prefix: "debundle-extract-plain-import-imported-superclass-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("keeps init wrappers for class declarations with retained superclass expressions", () => {
  const source = `console.log("before superclass install");
globalThis.RuntimeBaseBox = class RuntimeBaseBox {
  constructor(prefix) {
    this.prefix = prefix;
  }
};
class DerivedBox extends globalThis.RuntimeBaseBox {
  label() {
    return this.prefix + ":derived";
  }
}
function renderDerivedBox() {
  return new DerivedBox("ordered").label();
}
const result = renderDerivedBox();
console.log(result);
export { result };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperation(source, {
      init: "init_retained_superclass_region",
      ownerNames: ["DerivedBox", "renderDerivedBox"],
      targetFile: "regions/retained_superclass_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/retained_superclass_region.js");
  assert.match(runtimeCode, /import \{ DerivedBox, renderDerivedBox, init_retained_superclass_region \} from "\.\/regions\/retained_superclass_region\.js"/);
  assert.match(runtimeCode, /init_retained_superclass_region\(\);/);
  assert.match(extractedCode, /export function init_retained_superclass_region/);
  assert.match(extractedCode, /\bDerivedBox = class DerivedBox extends globalThis\.RuntimeBaseBox\b/);

  assertRunnableEquivalent({
    prefix: "debundle-extract-ordered-init-retained-superclass-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("naturalizes safe class declaration owners inside mixed init regions", () => {
  const source = `globalThis.accessorDeclarationEvents = [];
class AsyncAvatarAccessor {
  constructor(asyncNode, core) {
    this.asyncNode = asyncNode;
    this.core = core;
  }
  label() {
    return this.asyncNode + ":" + this.core;
  }
}
globalThis.accessorDeclarationEvents.push(new AsyncAvatarAccessor("node", "core").label());
console.log(globalThis.accessorDeclarationEvents.join("|"));
export { AsyncAvatarAccessor };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperationWithAttachedSideEffects(source, {
      attachedSideEffectIndexes: [1],
      init: "init_accessor_declaration_region",
      ownerNames: ["AsyncAvatarAccessor"],
      targetFile: "regions/accessor_declaration_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/accessor_declaration_region.js");
  assert.match(runtimeCode, /import \{ AsyncAvatarAccessor, init_accessor_declaration_region \} from "\.\/regions\/accessor_declaration_region\.js"/);
  assert.match(runtimeCode, /init_accessor_declaration_region\(\);/);
  assert.match(extractedCode, /^\s*class AsyncAvatarAccessor\b/m);
  assert.match(extractedCode, /export function init_accessor_declaration_region/);
  assert.doesNotMatch(extractedCode, /\blet AsyncAvatarAccessor\b/);
  assert.doesNotMatch(extractedCode, /\bAsyncAvatarAccessor = class AsyncAvatarAccessor\b/);

  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-naturalized-class-declaration-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("naturalizes safe function declaration owners inside mixed init regions", () => {
  const source = `globalThis.toolbarDeclarationEvents = [];
function NodeToolbarButton({ action, role, ...rest }) {
  return role + ":" + action + ":" + Object.keys(rest).length;
}
globalThis.toolbarDeclarationEvents.push(NodeToolbarButton({
  action: "open",
  role: "button",
  tabIndex: 0,
}));
console.log(globalThis.toolbarDeclarationEvents.join("|"));
export { NodeToolbarButton };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperationWithAttachedSideEffects(source, {
      attachedSideEffectIndexes: [1],
      init: "init_toolbar_declaration_region",
      ownerNames: ["NodeToolbarButton"],
      targetFile: "regions/toolbar_declaration_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/toolbar_declaration_region.js");
  assert.match(runtimeCode, /import \{ NodeToolbarButton, init_toolbar_declaration_region \} from "\.\/regions\/toolbar_declaration_region\.js"/);
  assert.match(runtimeCode, /init_toolbar_declaration_region\(\);/);
  assert.match(extractedCode, /^\s*function NodeToolbarButton\(\{\s*action,\s*role,\s*\.\.\.rest\s*\}\)/ms);
  assert.match(extractedCode, /export function init_toolbar_declaration_region/);
  assert.doesNotMatch(extractedCode, /\blet NodeToolbarButton\b/);
  assert.doesNotMatch(extractedCode, /\bNodeToolbarButton = function NodeToolbarButton\b/);

  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-naturalized-function-declaration-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("keeps init wrappers for class declarations with definition-time side effects", () => {
  const source = `globalThis.staticDeclarationEvents = [];
globalThis.staticDeclarationEvents.push("before");
class StaticDeclarationWidget {
  static value = globalThis.staticDeclarationEvents.push("static");
}
globalThis.staticDeclarationEvents.push("after:" + StaticDeclarationWidget.value);
console.log(globalThis.staticDeclarationEvents.join("|"));
export { StaticDeclarationWidget };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperationWithAttachedSideEffects(source, {
      attachedSideEffectIndexes: [2],
      init: "init_static_declaration_widget_region",
      ownerNames: ["StaticDeclarationWidget"],
      targetFile: "regions/static_declaration_widget_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/static_declaration_widget_region.js");
  assert.match(runtimeCode, /import \{ StaticDeclarationWidget, init_static_declaration_widget_region \} from "\.\/regions\/static_declaration_widget_region\.js"/);
  assert.match(runtimeCode, /init_static_declaration_widget_region\(\);/);
  assert.match(extractedCode, /\blet StaticDeclarationWidget\b/);
  assert.match(extractedCode, /\bStaticDeclarationWidget = class StaticDeclarationWidget\b/);
  assert.doesNotMatch(extractedCode, /^\s*class StaticDeclarationWidget\b/m);

  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-static-class-declaration-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("keeps class declaration init wrappers when earlier lazy code could observe TDZ", () => {
  const source = `function readLaterWidgetName() {
  return LaterWidget.name;
}
class LaterWidget {
  label() {
    return "later";
  }
}
globalThis.laterWidgetName = readLaterWidgetName();
console.log(new LaterWidget().label(), globalThis.laterWidgetName);
export { LaterWidget };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperationWithAttachedSideEffects(source, {
      attachedSideEffectIndexes: [0],
      init: "init_later_widget_region",
      ownerNames: ["readLaterWidgetName", "LaterWidget"],
      targetFile: "regions/later_widget_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/later_widget_region.js");
  assert.match(runtimeCode, /import \{ readLaterWidgetName, LaterWidget, init_later_widget_region \} from "\.\/regions\/later_widget_region\.js"/);
  assert.match(runtimeCode, /init_later_widget_region\(\);/);
  assert.match(extractedCode, /^\s*function readLaterWidgetName\(\)/m);
  assert.match(extractedCode, /\blet LaterWidget\b/);
  assert.match(extractedCode, /\bLaterWidget = class LaterWidget\b/);
  assert.doesNotMatch(extractedCode, /^\s*class LaterWidget\b/m);

  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-earlier-lazy-class-declaration-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("rejects class declaration extraction when earlier eager code observes TDZ", () => {
  const source = `console.log(LaterEagerWidget.name);
class LaterEagerWidget {}
`;
  assert.throws(
    () =>
      extractOrderedInitRegionsInCode(source, [
        selectedModuleOperation(source, {
          init: "init_later_eager_widget_region",
          ownerNames: ["LaterEagerWidget"],
          targetFile: "regions/later_eager_widget_region.js",
        }),
      ]),
    /would move binding LaterEagerWidget after eager use/
  );
});

test("naturalizes safe class-expression assignment owners inside mixed init regions", () => {
  const source = `globalThis.accessorEvents = [];
const AsyncAvatarAccessor = class AsyncAvatarAccessor {
  constructor(asyncNode, core) {
    this.asyncNode = asyncNode;
    this.core = core;
  }
  label() {
    return this.asyncNode + ":" + this.core;
  }
};
globalThis.accessorEvents.push(new AsyncAvatarAccessor("node", "core").label());
console.log(globalThis.accessorEvents.join("|"));
export { AsyncAvatarAccessor };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperationWithAttachedSideEffects(source, {
      attachedSideEffectIndexes: [1],
      init: "init_accessor_region",
      ownerNames: ["AsyncAvatarAccessor"],
      targetFile: "regions/accessor_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/accessor_region.js");
  assert.match(runtimeCode, /import \{ AsyncAvatarAccessor, init_accessor_region \} from "\.\/regions\/accessor_region\.js"/);
  assert.match(runtimeCode, /init_accessor_region\(\);/);
  assert.match(extractedCode, /\bclass AsyncAvatarAccessor\b/);
  assert.match(extractedCode, /export function init_accessor_region/);
  assert.doesNotMatch(extractedCode, /\blet AsyncAvatarAccessor\b/);
  assert.doesNotMatch(extractedCode, /\bAsyncAvatarAccessor = class AsyncAvatarAccessor\b/);

  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-naturalized-class-expression-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("naturalizes safe function-expression assignment owners inside mixed init regions", () => {
  const source = `globalThis.toolbarEvents = [];
const NodeToolbarButton = function NodeToolbarButton({ action, role, ...rest }) {
  return role + ":" + action + ":" + Object.keys(rest).length;
};
globalThis.toolbarEvents.push(NodeToolbarButton({
  action: "open",
  role: "button",
  tabIndex: 0,
}));
console.log(globalThis.toolbarEvents.join("|"));
export { NodeToolbarButton };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperationWithAttachedSideEffects(source, {
      attachedSideEffectIndexes: [1],
      init: "init_toolbar_region",
      ownerNames: ["NodeToolbarButton"],
      targetFile: "regions/toolbar_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/toolbar_region.js");
  assert.match(runtimeCode, /import \{ NodeToolbarButton, init_toolbar_region \} from "\.\/regions\/toolbar_region\.js"/);
  assert.match(runtimeCode, /init_toolbar_region\(\);/);
  assert.match(extractedCode, /\bfunction NodeToolbarButton\(\{\s*action,\s*role,\s*\.\.\.rest\s*\}\)/s);
  assert.match(extractedCode, /export function init_toolbar_region/);
  assert.doesNotMatch(extractedCode, /\blet NodeToolbarButton\b/);
  assert.doesNotMatch(extractedCode, /\bNodeToolbarButton = function NodeToolbarButton\b/);

  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-naturalized-function-expression-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("keeps init wrappers for class-expression assignments with definition-time side effects", () => {
  const source = `globalThis.staticWidgetEvents = [];
globalThis.staticWidgetEvents.push("before");
const StaticWidget = class StaticWidget {
  static value = globalThis.staticWidgetEvents.push("static");
};
globalThis.staticWidgetEvents.push("after:" + StaticWidget.value);
console.log(globalThis.staticWidgetEvents.join("|"));
export { StaticWidget };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperationWithAttachedSideEffects(source, {
      attachedSideEffectIndexes: [2],
      init: "init_static_widget_region",
      ownerNames: ["StaticWidget"],
      targetFile: "regions/static_widget_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/static_widget_region.js");
  assert.match(runtimeCode, /import \{ StaticWidget, init_static_widget_region \} from "\.\/regions\/static_widget_region\.js"/);
  assert.match(runtimeCode, /init_static_widget_region\(\);/);
  assert.match(extractedCode, /\blet StaticWidget\b/);
  assert.match(extractedCode, /export function init_static_widget_region/);
  assert.match(extractedCode, /\bconst StaticWidget = class StaticWidget\b/);
  assert.match(extractedCode, /\bStaticWidget = __dt_selected_module_snapshot__owner_00000\.StaticWidget\b/);
  assert.doesNotMatch(extractedCode, /^\s*class StaticWidget\b/m);

  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-static-class-expression-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("keeps init wrappers when earlier lazy code could observe declaration TDZ", () => {
  const source = `function readLaterButtonName() {
  return LaterButton.name;
}
const LaterButton = function LaterButton() {
  return "later";
};
globalThis.laterButtonName = readLaterButtonName();
console.log(LaterButton(), globalThis.laterButtonName);
export { LaterButton };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    selectedModuleOperationWithAttachedSideEffects(source, {
      attachedSideEffectIndexes: [0],
      init: "init_later_button_region",
      ownerNames: ["readLaterButtonName", "LaterButton"],
      targetFile: "regions/later_button_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/later_button_region.js");
  assert.match(runtimeCode, /import \{ readLaterButtonName, LaterButton, init_later_button_region \} from "\.\/regions\/later_button_region\.js"/);
  assert.match(runtimeCode, /init_later_button_region\(\);/);
  assert.match(extractedCode, /^\s*function readLaterButtonName\(\)/m);
  assert.match(extractedCode, /\blet LaterButton\b/);
  assert.doesNotMatch(extractedCode, /\blet readLaterButtonName\b/);
  assert.match(extractedCode, /\bLaterButton = function LaterButton\b/);
  assert.doesNotMatch(extractedCode, /^\s*function LaterButton\(\)/m);

  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-earlier-lazy-function-expression-",
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
    selectedModuleOperation(source, {
      init: "init_schema_region",
      ownerNames: ["leafSchema", "parseTree"],
      targetFile: "regions/schema_region.js",
    }),
  ]);

  const runtimeCode = result.files.get("runtime.js");
  const extractedCode = result.files.get("regions/schema_region.js");
  assert.match(runtimeCode, /\binit_schema_region\b/);
  assert.match(extractedCode, /__dt_selected_module_snapshot__/);
  assert.doesNotMatch(extractedCode, /__dt_selected_module_snapshot__\w+\s*=\s*\(\)\s*=>\s*\{/);

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
    selectedModuleOperation(source, {
      init: "init_lazy_schema_region",
      ownerNames: ["leafSchema"],
      targetFile: "regions/lazy_schema_region.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/lazy_schema_region.js");
  assert.doesNotMatch(extractedCode, /__dt_selected_module_snapshot__/);
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
      operation: "lower_selected_module_region",
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
    selectedModuleOperation(source, {
      init: "init_left_region",
      ownerNames: ["left"],
      targetFile: "regions/left_region.js",
    }),
    selectedModuleOperation(source, {
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
    selectedModuleOperation(source, {
      init: "init_state_region",
      ownerNames: ["open", "toggle", "isOpen", "initial", "next"],
      targetFile: "regions/state_region.js",
    }),
  ]);

  const extractedCode = result.files.get("regions/state_region.js");
  assert.match(extractedCode, /^\s*function toggle\(\)/m);
  assert.match(extractedCode, /^\s*function isOpen\(\)/m);
  assert.match(extractedCode, /let open, initial, next;/);
  assert.match(extractedCode, /export \{ open, toggle, isOpen, initial, next \};/);
  assert.match(extractedCode, /open = false/);
  assert.doesNotMatch(extractedCode, /toggle = function toggle/);

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
        selectedModuleOperation(source, {
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
        selectedModuleOperation(source, {
          init: "init_error_region",
          ownerNames: ["InvalidNodeIdError"],
          targetFile: "regions/error_region.js",
        }),
      ]),
    /would move binding InvalidNodeIdError after eager use/
  );
});

test("allows retained staged-shell statements to eagerly use earlier selected owners", () => {
  const source = `const helperSeed = 1;
const retained = helperSeed + 1;
const rendered = 3;
console.log(helperSeed, retained, rendered);
export { helperSeed, rendered };
`;
  const result = extractOrderedInitRegionsInCode(source, [
    {
      ...selectedModuleOperation(source, {
        init: "init_staged_earlier_use_region",
        ownerNames: ["helperSeed", "rendered"],
        targetFile: "regions/staged_earlier_use_region.js",
      }),
      lowering: "staged_shell",
    },
  ]);

  const runtimeCode = result.files.get("runtime.js");
  assert.match(runtimeCode, /const retained = helperSeed \+ 1;/);
  assertRunnableEquivalent({
    files: {},
    prefix: "debundle-extract-ordered-init-retained-earlier-use-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
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
          ...selectedModuleOperation(source, {
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

function selectedModuleOperation(source, { file = "runtime.js", init, ownerNames, targetFile }) {
  const ownerIds = ownerIdsForNames(source, ownerNames);
  return {
    id: `extract_${init}`,
    operation: "lower_selected_module_region",
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

function selectedModuleOperationWithAttachedSideEffects(
  source,
  { attachedSideEffectIndexes, file = "runtime.js", init, ownerNames, targetFile }
) {
  const analysis = analyzeRuntimeBoundaryCode(source, { chunkId: "static/app" });
  return {
    id: `extract_${init}`,
    operation: "lower_selected_module_region",
    selector: {
      attachedItemIds: sideEffectIdsForIndexes(analysis, attachedSideEffectIndexes),
      chunkId: "static/app",
      file,
      ownerIds: ownerIdsForNamesInAnalysis(analysis, ownerNames),
    },
    target: {
      file: targetFile,
      init,
    },
  };
}

function ownerIdsForNames(source, names) {
  const analysis = analyzeRuntimeBoundaryCode(source, { chunkId: "static/app" });
  return ownerIdsForNamesInAnalysis(analysis, names);
}

function ownerIdsForNamesInAnalysis(analysis, names) {
  return names.map((name) => {
    const owner = analysis.owners.find((candidate) => candidate.names.includes(name));
    if (!owner) {
      throw new Error(`Fixture owner not found for ${name}`);
    }
    return owner.id;
  });
}

function sideEffectIdsForIndexes(analysis, indexes) {
  return indexes.map((index) => {
    const sideEffect = analysis.sideEffects[index];
    if (!sideEffect) {
      throw new Error(`Fixture side effect not found for index ${index}`);
    }
    return sideEffect.id;
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
