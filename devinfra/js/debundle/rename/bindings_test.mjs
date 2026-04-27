import assert from "node:assert/strict";
import { join } from "node:path";
import test from "node:test";
import generateModule from "@babel/generator";
import { cloneDefaultParserOptions } from "../common/parser_options.mjs";
import { createFile, createArtifact, requireChunkFile } from "../common/artifact.mjs";
import {
  createTempFixtureRoot,
  makePipelineArtifact,
  makePipelineChunk,
  parseModuleCode,
  runNodeScript,
  writeRunnableFixture,
} from "../test_support/fixtures.mjs";
import { renameBindingsInArtifact, renameBindingsInCode } from "./bindings.mjs";

const generate = generateModule.default ?? generateModule;

const classSource = `class A {
  method() {
    const A = "shadow";
    return A.length;
  }
  static make() {
    return new A();
  }
}
function untouched(A) {
  return A + 1;
}
console.log(A.make().method(), untouched(4));
console.log(A.name);
export { A as publicA };
`;

test("renames class declarations without changing executable behavior", () => {
  const renamed = renameBindingsInCode(classSource, [classOperation()], {
    chunkManifest: classManifest(),
  }).code;

  assert.match(renamed, /class ReadableClass/);
  assert.match(renamed, /Object\.defineProperty\(ReadableClass, "name"/);
  assert.match(renamed, /new ReadableClass\(\)/);
  assert.match(renamed, /export \{ ReadableClass as publicA \}/);
  assert.match(renamed, /function untouched\(A\)/);
  assert.match(renamed, /const A = "shadow"/);
  assertRunnableEquivalent({ prefix: "debundle-rename-class-", renamed, source: classSource });
});

test("rejects target names already bound at program scope", () => {
  assert.throws(
    () =>
      renameBindingsInCode(
        `class A {}
const ReadableClass = 1;
export { A };
`,
        [
          renameOperation({
            id: "rename_A_collision",
            from: "A",
            to: "ReadableClass",
            owner: {
              id: "owner_00001",
              line: 1,
            },
          }),
        ],
        {
          chunkManifest: classManifest(),
        }
      ),
    /target ReadableClass already exists/
  );
});

test("rejects renames that would shadow nested references", () => {
  assert.throws(
    () =>
      renameBindingsInCode(
        `class A {
  method() {
    let ReadableClass;
    return A;
  }
}
export { A };
`,
        [
          renameOperation({
            id: "rename_A_shadow",
            from: "A",
            to: "ReadableClass",
            owner: {
              id: "owner_00001",
              line: 1,
            },
          }),
        ],
        {
          chunkManifest: classManifest(),
        }
      ),
    /would shadow ReadableClass/
  );
});

test("rejects bindings that do not match their fingerprint", () => {
  assert.throws(
    () =>
      renameBindingsInCode(
        classSource,
        [
          renameOperation({
            id: "rename_A_bad_fingerprint",
            from: "A",
            to: "ReadableClass",
            owner: {
              id: "owner_00001",
              line: 1,
            },
            fingerprint: {
              memberNamesPrefix: ["wrong"],
            },
          }),
        ],
        {
          chunkManifest: classManifest(),
        }
      ),
    /fingerprint/
  );
});

test("matches initStartsWith fingerprints across indentation-only drift", () => {
  const source = `const J5 = async n => {
  const e = Vh(n);
  if (!e) throw new Error(\`Cannot get account ID for \${n}\`);
  return e;
};
console.log(await J5("ok"));
`;
  const renamed = renameBindingsInCode(
    source,
    [
      renameOperation({
        id: "rename_J5_indent_normalized",
        from: "J5",
        to: "readAccountInfoFromMainRtdb",
        kind: "VariableDeclarator",
        owner: {
          id: "owner_00002",
          line: 1,
        },
        fingerprint: {
          initStartsWith: "async n => {\n    const e = Vh(n);",
        },
      }),
    ],
    {
      chunkManifest: variableDeclaratorManifest({
        id: "owner_00002",
        line: 1,
        names: ["J5"],
      }),
    }
  ).code;

  assert.match(renamed, /const readAccountInfoFromMainRtdb = async n =>/);
  assert.match(renamed, /console\.log\(await readAccountInfoFromMainRtdb\("ok"\)\)/);
});

test("matches initStartsWith fingerprints across object-literal line wrapping", () => {
  const source = `const Cm = {
  notStarted: 0,
  started: -1,
  finished: -2,
  disabled: -3,
  invalid: -4
};
console.log(Cm.started, Cm.finished);
`;
  const renamed = renameBindingsInCode(
    source,
    [
      renameOperation({
        id: "rename_Cm_action_event_phase",
        from: "Cm",
        to: "actionEventPhase",
        kind: "VariableDeclarator",
        owner: {
          id: "owner_00002",
          line: 1,
        },
        fingerprint: {
          initStartsWith: "{ notStarted: 0, started: -1, finished: -2, disabled: -3, invalid: -4 }",
        },
      }),
    ],
    {
      chunkManifest: variableDeclaratorManifest({
        id: "owner_00002",
        line: 1,
        names: ["Cm"],
      }),
    }
  ).code;

  assert.match(renamed, /const actionEventPhase = \{/);
  assert.match(renamed, /console\.log\(actionEventPhase\.started, actionEventPhase\.finished\)/);
});

test("renames external import aliases without changing executable behavior", () => {
  const importSource = `import { j as a } from "./vendor.js";
function render() {
  const local = "ok";
  return [a.jsx("div"), local];
}
console.log(render()[0].type, render()[1]);
export { a as publicJsx };
`;
  const renamed = renameBindingsInCode(importSource, [jsxRuntimeImportOperation()], {
    chunkManifest: importManifest(),
  }).code;

  assert.match(renamed, /import \{ j as jsxRuntime \}/);
  assert.match(renamed, /jsxRuntime\.jsx\("div"\)/);
  assert.match(renamed, /export \{ jsxRuntime as publicJsx \}/);
  assertRunnableEquivalent({
    files: {
      "vendor.js": `export const j = { jsx(type) { return { type }; } };\n`,
    },
    prefix: "debundle-rename-import-",
    renamed,
    source: importSource,
  });
});

test("renames generated split-part import aliases without changing executable behavior", () => {
  const source = `import { bZ } from "./parts/part-0001.js";
function render(content) {
  return bZ(content);
}
console.log(render([{ type: "text", text: "ok" }]));
`;
  const renamed = renameBindingsInCode(source, [splitPartImportOperation()], {
    chunkManifest: splitPartImportManifest(),
  }).code;

  assert.match(renamed, /import \{ bZ as mcpContentToText \}/);
  assert.match(renamed, /return mcpContentToText\(content\)/);
  assertRunnableEquivalent({
    files: {
      "parts/part-0001.js": `export function bZ(content) { return content.map((item) => item.text).join(""); }\n`,
    },
    prefix: "debundle-rename-split-part-import-",
    renamed,
    source,
  });
});

test("renameBindingsInCode treats the manifest entryFile as the implicit part-import shell", () => {
  const renamed = renameBindingsInCode(
    `import { bZ } from "./parts/part-0001.js";\nconsole.log(bZ);\n`,
    [splitPartImportOperation({ file: "entry.js" })],
    {
      chunkManifest: splitPartImportEntryManifest(),
      file: "entry.js",
    }
  ).code;

  assert.match(renamed, /import \{ bZ as mcpContentToText \}/);
  assert.match(renamed, /console\.log\(mcpContentToText\)/);
});

test("renameBindingsInCode matches split entry import sources by chunk, not literal runtime.js", () => {
  const renamed = renameBindingsInCode(
    `import { j as a } from "./vendor/entry.js";\nconsole.log(a.jsx("div"));\n`,
    [jsxRuntimeImportOperation({ file: "entry.js" })],
    {
      chunkManifest: entryImportManifest(),
      file: "entry.js",
    }
  ).code;

  assert.match(renamed, /import \{ j as jsxRuntime \} from "\.\/vendor\/entry\.js"/);
  assertRunnableEquivalent({
    files: {
      "vendor/entry.js": `export const j = { jsx(type) { return { type }; } };\n`,
    },
    prefix: "debundle-rename-entry-import-",
    renamed,
    source: `import { j as a } from "./vendor/entry.js";\nconsole.log(a.jsx("div"));\n`,
  });
});

test("renameBindingsInArtifact works without precomputed manifests and with no emitted parts", () => {
  const artifact = createArtifact({
    chunks: [
      {
        chunkId: "static/app",
        entryFile: "runtime.js",
        files: [
          createFile({
            path: "runtime.js",
            ast: parseModuleCode(classSource),
            parserOptions: cloneDefaultParserOptions(),
            metadata: {
              chunkId: "static/app",
              chunkFile: "runtime.js",
              role: "entry",
            },
          }),
        ],
      },
    ],
  });

  renameBindingsInArtifact({
    artifact,
    operations: [classOperation()],
  });

  const renamed = generate(requireChunkFile(artifact, "static/app", "runtime.js", "renameBindingsNoManifest").ast).code;
  assert.match(renamed, /class ReadableClass/);
  assert.doesNotMatch(renamed, /parts\/part-/);
  assertRunnableEquivalent({ prefix: "debundle-rename-no-manifest-", renamed, source: classSource });
});

test("rejects import alias renames that would shadow nested references", () => {
  assert.throws(
    () =>
      renameBindingsInCode(
        `import { j as a } from "./vendor.js";
function render() {
  const jsxRuntime = "shadow";
  return a.jsx("div") || jsxRuntime;
}
`,
        [jsxRuntimeImportOperation()],
        {
          chunkManifest: importManifest(),
        }
      ),
    /would shadow jsxRuntime/
  );
});

test("renames variable declarators and exports", () => {
  const source = `const X = 1, unused = 2;
function total() {
  return X + unused;
}
console.log(total());
export { X as publicX };
`;
  const renamed = renameBindingsInCode(source, [variableOperation("rename_X")], {
    chunkManifest: variableManifest(),
  }).code;

  assert.match(renamed, /const ReadableValue = 1/);
  assert.match(renamed, /return ReadableValue \+ unused/);
  assert.match(renamed, /export \{ ReadableValue as publicX \}/);
});

test("renameBindingsInCode renames plain declarations without requiring manifest or implicit runtime file", () => {
  const source = `const X = 1, unused = 2;
function total() {
  return X + unused;
}
console.log(total());
export { X as publicX };
`;
  const renamed = renameBindingsInCode(source, [variableOperation("rename_X")]).code;

  assert.match(renamed, /const ReadableValue = 1/);
  assert.match(renamed, /return ReadableValue \+ unused/);
  assert.match(renamed, /export \{ ReadableValue as publicX \}/);
  assertRunnableEquivalent({ prefix: "debundle-rename-no-file-", renamed, source });
});

test("renames assignments and preserves object shorthand keys", () => {
  const source = `let X = 1;
X += 2;
X++;
console.log(JSON.stringify({ X }), X);
export { X as publicX };
`;
  const renamed = renameBindingsInCode(source, [variableOperation("rename_mutated_X")], {
    chunkManifest: variableManifest(),
  }).code;

  assert.match(renamed, /ReadableValue \+= 2/);
  assert.match(renamed, /ReadableValue\+\+/);
  assert.match(renamed, /X: ReadableValue/);
  assertRunnableEquivalent({ prefix: "debundle-rename-mutation-", renamed, source });
});

test("renames function declarations and exports", () => {
  const source = `function i(r, e) {
  return r + e;
}
console.log(i(1, 2));
export { i as add };
`;
  const renamed = renameBindingsInCode(
    source,
    [
      renameOperation({
        id: "rename_i",
        from: "i",
        to: "addNumbers",
        kind: "FunctionDeclaration",
        owner: {
          id: "owner_00003",
          line: 1,
        },
        fingerprint: {
          bodyContains: "return r + e;",
          paramsCount: 2,
        },
      }),
    ],
    {
      chunkManifest: functionManifest(),
    }
  ).code;

  assert.match(renamed, /function addNumbers\(r, e\)/);
  assert.match(renamed, /console\.log\(addNumbers\(1, 2\)\)/);
  assert.match(renamed, /export \{ addNumbers as add \}/);
});

test("applies batched renames with pre-rename fingerprints", () => {
  const source = `const a = 1;
function b() {
  return a + 1;
}
console.log(b());
`;
  const renamed = renameBindingsInCode(
    source,
    [
      renameOperation({
        id: "rename_batch_a",
        from: "a",
        to: "readableA",
        kind: "VariableDeclarator",
        owner: {
          id: "owner_00004",
          line: 1,
        },
        fingerprint: {
          initEquals: "1",
        },
      }),
      renameOperation({
        id: "rename_batch_b",
        from: "b",
        to: "readableB",
        kind: "FunctionDeclaration",
        owner: {
          id: "owner_00005",
          line: 2,
        },
        fingerprint: {
          bodyContains: "a + 1",
          paramsCount: 0,
        },
      }),
    ],
    {
      chunkManifest: sequentialBatchManifest(),
    }
  ).code;

  assert.match(renamed, /const readableA = 1/);
  assert.match(renamed, /function readableB\(\)/);
  assert.match(renamed, /return readableA \+ 1/);
  assertRunnableEquivalent({ prefix: "debundle-rename-sequential-batch-", renamed, source });
});

test("rejects ambiguous top-level binding matches", () => {
  assert.throws(
    () =>
      renameBindingsInCode(
        `var x = 1;
var x = 2;
console.log(x);
`,
        [
          renameOperation({
            id: "rename_duplicate_x",
            from: "x",
            to: "readableX",
            kind: "VariableDeclarator",
          }),
        ]
      ),
    /matched 2 top-level bindings/
  );
});

test("renames snapshots while preserving untouched split part files", () => {
  const artifact = makePipelineArtifact(
    [
      makePipelineChunk(
        "static/app",
        {
          "runtime.js": classSource,
          "parts/part-0001.js": `export function helper(){return 1;}\n`,
        },
        {
          manifest: classManifest(),
        }
      ),
    ],
    {
      manifest: {
        schemaVersion: 1,
        uiVersion: "fixture",
        counts: {
          chunks: 1,
        },
      },
    }
  );
  const originalPart = generate(
    requireChunkFile(artifact, "static/app", "parts/part-0001.js", "renameBindingsTest").ast,
    { comments: true }
  ).code;

  const { artifact: renamedArtifact, manifest: snapshotManifest } = renameBindingsInArtifact({
    artifact,
    operations: [classOperation()],
  });

  assert.equal(snapshotManifest.counts.bindingRenames, 1);
  assert.equal(snapshotManifest.renames[0].from, "A");
  assert.equal(snapshotManifest.renames[0].to, "ReadableClass");
  assert.match(
    generate(requireChunkFile(renamedArtifact, "static/app", "runtime.js", "renameBindingsTest").ast, {
      comments: true,
    }).code,
    /class ReadableClass/
  );
  assert.equal(
    generate(requireChunkFile(renamedArtifact, "static/app", "parts/part-0001.js", "renameBindingsTest").ast, {
      comments: true,
    }).code,
    originalPart
  );
});

function classManifest() {
  return chunkManifest({
    chunkId: "static/app",
    owner: {
      id: "owner_00001",
      line: 1,
      names: ["A"],
      type: "ClassDeclaration",
      unsafeReason: "unsupported_top_level_declaration:ClassDeclaration",
    },
  });
}

function classOperation() {
  return renameOperation({
    id: "rename_A",
    from: "A",
    to: "ReadableClass",
    owner: {
      id: "owner_00001",
      line: 1,
    },
    fingerprint: {
      memberNamesPrefix: ["method", "static make"],
      superClass: null,
    },
  });
}

function importManifest() {
  return chunkManifest({
    chunkId: "static/app",
    imports: [
      {
        id: "import_00000",
        line: 1,
        source: "./vendor.js",
        specifiers: [
          {
            imported: "j",
            kind: "named",
            local: "a",
          },
        ],
      },
    ],
  });
}

function entryImportManifest() {
  return chunkManifest({
    chunkId: "static/app",
    entryFile: "entry.js",
    imports: [
      {
        id: "import_00000",
        line: 1,
        source: "./vendor.js",
        specifiers: [
          {
            imported: "j",
            kind: "named",
            local: "a",
          },
        ],
      },
    ],
  });
}

function jsxRuntimeImportOperation({ file = "runtime.js" } = {}) {
  return renameOperation({
    id: "rename_jsx_runtime",
    from: "a",
    to: "jsxRuntime",
    file,
    kind: "ImportSpecifier",
    importSelector: {
      imported: "j",
      source: "./vendor.js",
    },
    fingerprint: {
      imported: "j",
      local: "a",
      source: "./vendor.js",
    },
  });
}

function splitPartImportManifest() {
  return chunkManifest({
    chunkId: "static/app",
    parts: [
      {
        exportedNames: ["bZ"],
        file: "parts/part-0001.js",
      },
    ],
  });
}

function splitPartImportEntryManifest() {
  return chunkManifest({
    chunkId: "static/app",
    entryFile: "entry.js",
    parts: [
      {
        exportedNames: ["bZ"],
        file: "parts/part-0001.js",
      },
    ],
  });
}

function splitPartImportOperation({ file = "runtime.js" } = {}) {
  return renameOperation({
    id: "rename_split_part_import",
    from: "bZ",
    to: "mcpContentToText",
    file,
    kind: "ImportSpecifier",
    importSelector: {
      imported: "bZ",
      source: "./parts/part-0001.js",
    },
    fingerprint: {
      imported: "bZ",
      local: "bZ",
      source: "./parts/part-0001.js",
    },
  });
}

function variableManifest() {
  return chunkManifest({
    chunkId: "static/app",
    owner: {
      id: "owner_00002",
      line: 1,
      names: ["X", "unused"],
      type: "VariableDeclaration",
      unsafeReason: "unsupported_top_level_declaration:VariableDeclaration",
    },
  });
}

function variableDeclaratorManifest({ id, line, names }) {
  return chunkManifest({
    chunkId: "static/app",
    owner: {
      id,
      line,
      names,
      type: "VariableDeclaration",
      unsafeReason: "unsupported_top_level_declaration:VariableDeclaration",
    },
  });
}

function variableOperation(id) {
  return renameOperation({
    id,
    from: "X",
    to: "ReadableValue",
    kind: "VariableDeclarator",
    owner: {
      id: "owner_00002",
      line: 1,
    },
    fingerprint: {
      initEquals: "1",
    },
  });
}

function functionManifest() {
  return chunkManifest({
    chunkId: "static/app",
    owner: {
      id: "owner_00003",
      line: 1,
      names: ["i"],
      type: "FunctionDeclaration",
      unsafeReason: "depends_on_unsplit_top_level_binding:q",
    },
  });
}

function sequentialBatchManifest() {
  return chunkManifest({
    chunkId: "static/app",
    owners: [
      {
        id: "owner_00004",
        line: 1,
        names: ["a"],
        type: "VariableDeclaration",
      },
      {
        id: "owner_00005",
        line: 2,
        names: ["b"],
        type: "FunctionDeclaration",
      },
    ],
  });
}

function renameOperation({ id, from, to, file = "runtime.js", owner, fingerprint, importSelector, kind = "ClassDeclaration" }) {
  return {
    id,
    operation: "rename_binding",
    selector: {
      binding: {
        kind,
        name: from,
      },
      chunkId: "static/app",
      file,
      ...(importSelector ? { import: importSelector } : {}),
      ...(owner ? { owner } : {}),
    },
    target: {
      name: to,
    },
    ...(fingerprint ? { fingerprint } : {}),
  };
}

function chunkManifest({ chunkId, entryFile = "runtime.js", owner, owners, imports = [], parts = [] }) {
  return {
    schemaVersion: 1,
    chunkId,
    entryFile,
    parser: cloneDefaultParserOptions(),
    imports,
    keptTopLevelDeclarations: owners ?? (owner ? [owner] : []),
    parts,
  };
}

function assertRunnableEquivalent({ files = {}, prefix, renamed, source }) {
  const runnableDir = createTempFixtureRoot(prefix);
  writeRunnableFixture(runnableDir, {
    files: {
      ...files,
      "original.js": source,
      "renamed.js": renamed,
    },
  });
  assert.deepEqual(runNodeScript(join(runnableDir, "renamed.js")), runNodeScript(join(runnableDir, "original.js")));
}
