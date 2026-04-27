import assert from "node:assert/strict";
import test from "node:test";
import {
  ensureArtifactExtras,
  getArtifactVendorAnnotations,
  listChunkIds,
  requireChunkFile,
} from "../common/artifact.mjs";
import { makePipelineArtifact, makePipelineChunk } from "../test_support/fixtures.mjs";
import { applyVendorAnnotations } from "./annotate.mjs";

function fakeArtifact({ chunkIds = ["static/pdf.worker-U2G3g_2t", "static/index-AppEntry"], extra = {} } = {}) {
  const artifact = makePipelineArtifact(
    chunkIds.map((chunkId) =>
      makePipelineChunk(chunkId, {
        "runtime.js": { ast: { sentinel: chunkId } },
      })
    ),
    {
      annotations: extra.annotations,
      manifest: extra.manifest,
      uiVersion: extra.uiVersion,
    }
  );
  const extras = ensureArtifactExtras(artifact);
  for (const [key, value] of Object.entries(extra.annotations ?? {})) {
    if (key !== "vendor") {
      extras.annotations[key] = value;
    }
  }
  return artifact;
}

function vendorOp(overrides = {}) {
  return {
    id: "mark_vendor_pdfjs_worker",
    operation: "mark_vendor",
    level: "suppress",
    chunkPath: "static/pdf.worker-U2G3g_2t.js",
    identity: "pdfjs-dist/build/pdf.worker",
    upstreamFamily: "PDF.js",
    version: "3.2.146",
    confidence: "confirmed",
    evidence: [{ path: "static/pdf.worker-U2G3g_2t.js", line: 95, text: 'const workerVersion = "3.2.146"' }],
    ...overrides,
  };
}

test("happy path: single op annotates the right chunk only", () => {
  const artifact = fakeArtifact();
  const chunksRef = artifact.chunks;
  const chunkKeysBefore = listChunkIds(artifact);

  const { artifact: out, manifest } = applyVendorAnnotations({
    artifact,
    operations: [vendorOp()],
  });

  assert.equal(out, artifact);
  assert.equal(out.chunks, chunksRef);
  assert.deepEqual(listChunkIds(out), chunkKeysBefore);

  const vendor = getArtifactVendorAnnotations(out);
  assert.ok(vendor instanceof Map);
  assert.equal(vendor.size, 1);
  const annotation = vendor.get("static/pdf.worker-U2G3g_2t");
  assert.equal(annotation.identity, "pdfjs-dist/build/pdf.worker");
  assert.equal(annotation.chunkId, "static/pdf.worker-U2G3g_2t");
  assert.equal(annotation.chunkPath, "static/pdf.worker-U2G3g_2t.js");
  assert.equal(annotation.upstreamFamily, "PDF.js");
  assert.equal(annotation.version, "3.2.146");
  assert.equal(vendor.has("static/index-AppEntry"), false);

  assert.equal(manifest.kind, "js.vendor_annotations_manifest");
  assert.equal(manifest.counts.annotations, 1);
  assert.equal(manifest.counts.applied, 1);
  assert.equal(manifest.annotations.length, 1);
  assert.equal(manifest.annotations[0].id, "mark_vendor_pdfjs_worker");
});

test("multiple ops on distinct chunks attach each annotation", () => {
  const artifact = fakeArtifact({
    chunkIds: ["static/a", "static/b", "static/c"],
  });
  const ops = [
    vendorOp({ id: "op_a", chunkPath: "static/a.js", identity: "lib-a" }),
    vendorOp({ id: "op_b", chunkPath: "static/b.js", identity: "lib-b" }),
  ];
  const { manifest, artifact: out } = applyVendorAnnotations({ artifact, operations: ops });

  const vendor = getArtifactVendorAnnotations(out);
  assert.equal(vendor.size, 2);
  assert.equal(vendor.get("static/a").identity, "lib-a");
  assert.equal(vendor.get("static/b").identity, "lib-b");
  assert.equal(vendor.has("static/c"), false);
  assert.deepEqual(
    manifest.annotations.map((entry) => entry.id).sort(),
    ["op_a", "op_b"]
  );
});

test("chunkPath maps to chunkId by stripping .js", () => {
  const artifact = fakeArtifact({ chunkIds: ["static/pdf.worker-U2G3g_2t"] });
  const { artifact: out } = applyVendorAnnotations({
    artifact,
    operations: [vendorOp()],
  });
  assert.ok(getArtifactVendorAnnotations(out).has("static/pdf.worker-U2G3g_2t"));
});

test("missing chunk in artifact throws naming op id and path", () => {
  const artifact = fakeArtifact({ chunkIds: ["static/other"] });
  assert.throws(
    () => applyVendorAnnotations({ artifact, operations: [vendorOp()] }),
    (err) =>
      /mark_vendor_pdfjs_worker/.test(err.message) &&
      /static\/pdf\.worker-U2G3g_2t\.js/.test(err.message) &&
      /missing chunk/.test(err.message)
  );
});

test("duplicate chunkPath across two ops throws naming both ids", () => {
  const artifact = fakeArtifact({ chunkIds: ["static/dup"] });
  const ops = [
    vendorOp({ id: "op_first", chunkPath: "static/dup.js", identity: "lib-x" }),
    vendorOp({ id: "op_second", chunkPath: "static/dup.js", identity: "lib-x" }),
  ];
  assert.throws(
    () => applyVendorAnnotations({ artifact, operations: ops }),
    (err) => /op_first/.test(err.message) && /op_second/.test(err.message) && /static\/dup\.js/.test(err.message)
  );
});

test("missing required field throws naming op id and field", () => {
  const artifact = fakeArtifact({ chunkIds: ["static/pdf.worker-U2G3g_2t"] });
  const op = vendorOp();
  delete op.identity;
  assert.throws(
    () => applyVendorAnnotations({ artifact, operations: [op] }),
    (err) => /mark_vendor_pdfjs_worker/.test(err.message) && /identity/.test(err.message)
  );
});

test('level "boundary-rename" is accepted and fields propagate', () => {
  const artifact = fakeArtifact({ chunkIds: ["static/katex-BZy9Y_85"] });
  const op = vendorOp({
    id: "op_br",
    chunkPath: "static/katex-BZy9Y_85.js",
    level: "boundary-rename",
    package: "katex",
    version: "0.16.11",
    subpath: "dist/katex.mjs",
    exportShape: { render: "top-level named export", version: "string constant" },
  });
  const { artifact: out, manifest } = applyVendorAnnotations({
    artifact,
    operations: [op],
  });
  const annotation = getArtifactVendorAnnotations(out).get("static/katex-BZy9Y_85");
  assert.equal(annotation.level, "boundary-rename");
  assert.equal(annotation.package, "katex");
  assert.equal(annotation.version, "0.16.11");
  assert.equal(annotation.subpath, "dist/katex.mjs");
  assert.equal(annotation.role, "module");
  assert.deepEqual(annotation.exportShape, { render: "top-level named export", version: "string constant" });
  assert.equal(manifest.annotations[0].package, "katex");
});

test('level "swap" is accepted when package, version, subpath provided', () => {
  const artifact = fakeArtifact({ chunkIds: ["static/katex-BZy9Y_85"] });
  const op = vendorOp({
    id: "op_swap",
    chunkPath: "static/katex-BZy9Y_85.js",
    level: "swap",
    package: "katex",
    version: "0.16.11",
    subpath: "dist/katex.mjs",
    fingerprint: { algorithm: "sha256", hash: "deadbeef" },
  });
  const { artifact: out } = applyVendorAnnotations({
    artifact,
    operations: [op],
  });
  const annotation = getArtifactVendorAnnotations(out).get("static/katex-BZy9Y_85");
  assert.equal(annotation.level, "swap");
  assert.equal(annotation.package, "katex");
  assert.equal(annotation.version, "0.16.11");
  assert.equal(annotation.subpath, "dist/katex.mjs");
  assert.deepEqual(annotation.fingerprint, { algorithm: "sha256", hash: "deadbeef" });
});

test('level "swap" without package/version/subpath fails closed', () => {
  const artifact = fakeArtifact({ chunkIds: ["static/x"] });
  for (const missing of ["package", "version", "subpath"]) {
    const op = vendorOp({
      id: "op_swap_miss",
      chunkPath: "static/x.js",
      level: "swap",
      package: "katex",
      version: "0.16.11",
      subpath: "dist/katex.mjs",
    });
    delete op[missing];
    assert.throws(
      () => applyVendorAnnotations({ artifact, operations: [op] }),
      (err) => /op_swap_miss/.test(err.message) && /swap/.test(err.message) && new RegExp(missing).test(err.message),
      `expected failure naming missing field ${missing}`
    );
  }
});

test("unknown level rejects naming the op id", () => {
  const artifact = fakeArtifact({ chunkIds: ["static/x"] });
  const op = vendorOp({ id: "op_unknown", chunkPath: "static/x.js", level: "noodle" });
  assert.throws(
    () => applyVendorAnnotations({ artifact, operations: [op] }),
    (err) => /op_unknown/.test(err.message) && /noodle/.test(err.message) && /unknown level/.test(err.message)
  );
});

test('role "worker" accepted; default is "module"', () => {
  const artifact = fakeArtifact({ chunkIds: ["static/pdf.worker-U2G3g_2t"] });
  const op = vendorOp({ role: "worker" });
  const { artifact: out } = applyVendorAnnotations({
    artifact,
    operations: [op],
  });
  assert.equal(getArtifactVendorAnnotations(out).get("static/pdf.worker-U2G3g_2t").role, "worker");
});

test("empty evidence array throws", () => {
  const artifact = fakeArtifact({ chunkIds: ["static/pdf.worker-U2G3g_2t"] });
  const op = vendorOp({ evidence: [] });
  assert.throws(
    () => applyVendorAnnotations({ artifact, operations: [op] }),
    (err) => /mark_vendor_pdfjs_worker/.test(err.message) && /evidence/.test(err.message)
  );
});

test("non-mark_vendor entries in operations are ignored silently", () => {
  const artifact = fakeArtifact({ chunkIds: ["static/pdf.worker-U2G3g_2t"] });
  const ops = [
    {
      id: "rename_X",
      operation: "rename_binding",
      selector: { chunkId: "static/pdf.worker-U2G3g_2t", binding: { name: "X", kind: "ClassDeclaration" } },
      target: { name: "Y" },
    },
    vendorOp(),
    { id: "noop", operation: "something_else" },
  ];
  const { manifest, artifact: out } = applyVendorAnnotations({ artifact, operations: ops });
  assert.equal(getArtifactVendorAnnotations(out).size, 1);
  assert.equal(manifest.counts.applied, 1);
  assert.equal(manifest.counts.considered, 3);
});

test("AST sentinels are never mutated", () => {
  const artifact = fakeArtifact();
  const sentinelBefore = requireChunkFile(artifact, "static/pdf.worker-U2G3g_2t", "runtime.js").ast;
  applyVendorAnnotations({ artifact, operations: [vendorOp()] });
  const sentinelAfter = requireChunkFile(artifact, "static/pdf.worker-U2G3g_2t", "runtime.js").ast;
  assert.equal(sentinelBefore, sentinelAfter);
});

test("pre-existing annotations are preserved alongside vendor", () => {
  const other = { keep: "me" };
  const artifact = fakeArtifact({ extra: { annotations: { other } } });
  const { artifact: out } = applyVendorAnnotations({
    artifact,
    operations: [vendorOp()],
  });
  assert.equal(out.extras.annotations.other, other);
  assert.equal(out.extras.annotations.other.keep, "me");
  const vendor = getArtifactVendorAnnotations(out);
  assert.ok(vendor instanceof Map);
  assert.equal(vendor.size, 1);
});
