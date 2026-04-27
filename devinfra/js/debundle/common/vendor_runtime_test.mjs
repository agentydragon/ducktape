import assert from "node:assert/strict";
import { join } from "node:path";
import test from "node:test";

import { createWebFixtureRoots } from "../test_support/fixtures.mjs";
import { writeJsonFile, writeTextFile } from "./parser_options.mjs";
import { loadVendorRuntimeIndex, resolveVendorRuntimeRequest } from "./vendor_runtime.mjs";

test("vendor runtime index resolves explicit entry files and nested mount paths", () => {
  const fixture = createWebFixtureRoots("debundle-vendor-runtime-entry-file-");
  const manifestPath = join(fixture.vendorsRoot, "manifest.json");

  writeTextFile(join(fixture.packagesRoot, "fflate", "esm", "browser.js"), 'export const version = "0.8.2";\n');
  writeTextFile(join(fixture.packagesRoot, "fflate", "esm", "helpers", "helper.js"), "export const helper = 1;\n");
  writeJsonFile(join(fixture.packagesRoot, "fflate", "package.json"), {
    name: "fflate",
    version: "0.8.2",
  });

  writeTextFile(
    join(fixture.vendorsRoot, "generated", "static", "native-B5Vb9Oiz", "entry.js"),
    "export default { native: true };\n"
  );

  writeJsonFile(manifestPath, {
    kind: "js.vendor_resolution_manifest",
    resolutions: {
      "static/browser-DohgXfL6.js": {
        chunkId: "static/browser-DohgXfL6",
        chunkPath: "static/browser-DohgXfL6.js",
        entryFile: "esm/browser.js",
        package: "fflate",
        version: "0.8.2",
        subpath: "esm/browser.js",
      },
      "static/native-B5Vb9Oiz.js": {
        chunkId: "static/native-B5Vb9Oiz",
        chunkPath: "static/native-B5Vb9Oiz.js",
        entryFile: "entry.js",
        package: "@emoji-mart/data",
        version: "1.2.1",
        subpath: "sets/15/native.json",
        generatedWrapperPath: "vendors/generated/static/native-B5Vb9Oiz/entry.js",
        wrapperShape: "named-from-json-default",
      },
    },
  });

  const index = loadVendorRuntimeIndex({
    manifestPath,
    outRoot: fixture.root,
    packagesRoot: fixture.packagesRoot,
  });

  const nestedEntry = resolveVendorRuntimeRequest("app/static/browser-DohgXfL6/esm/browser.js", index);
  assert.equal(nestedEntry?.chunkId, "static/browser-DohgXfL6");
  assert.ok(nestedEntry?.filePath.endsWith("/node_modules/fflate/esm/browser.js"));

  const nestedSibling = resolveVendorRuntimeRequest("app/static/browser-DohgXfL6/esm/helpers/helper.js", index);
  assert.equal(nestedSibling?.chunkId, "static/browser-DohgXfL6");
  assert.ok(nestedSibling?.filePath.endsWith("/node_modules/fflate/esm/helpers/helper.js"));

  const wrapperEntry = resolveVendorRuntimeRequest("app/static/native-B5Vb9Oiz/entry.js", index);
  assert.equal(wrapperEntry?.chunkId, "static/native-B5Vb9Oiz");
  assert.ok(wrapperEntry?.filePath.endsWith("/vendors/generated/static/native-B5Vb9Oiz/entry.js"));

  const wrapperLegacyAlias = resolveVendorRuntimeRequest("app/static/native-B5Vb9Oiz/runtime.js", index);
  assert.equal(wrapperLegacyAlias?.chunkId, "static/native-B5Vb9Oiz");
  assert.ok(wrapperLegacyAlias?.filePath.endsWith("/vendors/generated/static/native-B5Vb9Oiz/entry.js"));
});

test("vendor runtime index requires explicit entryFile in the resolution manifest", () => {
  const fixture = createWebFixtureRoots("debundle-vendor-runtime-requires-entry-file-");
  const manifestPath = join(fixture.vendorsRoot, "manifest.json");

  writeTextFile(join(fixture.packagesRoot, "katex", "dist", "katex.mjs"), 'export const render = () => "katex";\n');
  writeJsonFile(join(fixture.packagesRoot, "katex", "package.json"), {
    name: "katex",
    version: "0.16.19",
  });

  writeJsonFile(manifestPath, {
    kind: "js.vendor_resolution_manifest",
    resolutions: {
      "static/katex-BZy9Y_85.js": {
        chunkId: "static/katex-BZy9Y_85",
        chunkPath: "static/katex-BZy9Y_85.js",
        package: "katex",
        version: "0.16.19",
        subpath: "dist/katex.mjs",
      },
    },
  });

  assert.throws(
    () =>
      loadVendorRuntimeIndex({
        manifestPath,
        outRoot: fixture.root,
        packagesRoot: fixture.packagesRoot,
      }),
    /missing entryFile/
  );
});
