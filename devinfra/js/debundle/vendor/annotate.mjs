import { getArtifactVendorAnnotations, getChunkEntryFile, requirePipelineArtifact } from "../common/artifact.mjs";

const REQUIRED_FIELDS = ["id", "operation", "level", "chunkPath", "identity", "evidence"];
const VALID_LEVELS = new Set(["suppress", "boundary-rename", "swap"]);
const SWAP_REQUIRED = ["package", "version", "subpath"];

export function applyVendorAnnotations({ artifact, operations, operationCatalog }) {
  requirePipelineArtifact(artifact, "applyVendorAnnotations");
  const catalog = operations ?? operationCatalog ?? [];
  const ops = catalog.filter((op) => op?.operation === "mark_vendor");

  const seenChunkPaths = new Map();
  const annotations = getArtifactVendorAnnotations(artifact);
  annotations.clear();
  const summaries = [];

  for (const op of ops) {
    validateOp(op);
    const chunkId = chunkIdFromChunkPath(op.chunkPath, op.id);
    if (!getChunkEntryFile(artifact, chunkId)) {
      throw new Error(
        `mark_vendor operation ${op.id} targets missing chunk: chunkPath=${op.chunkPath} (chunkId=${chunkId})`
      );
    }
    if (seenChunkPaths.has(op.chunkPath)) {
      const priorId = seenChunkPaths.get(op.chunkPath);
      throw new Error(`mark_vendor operations ${priorId} and ${op.id} both target chunkPath ${op.chunkPath}`);
    }
    seenChunkPaths.set(op.chunkPath, op.id);

    const annotation = {
      id: op.id,
      level: op.level,
      chunkPath: op.chunkPath,
      chunkId,
      identity: op.identity,
      evidence: op.evidence,
      ...(op.upstreamFamily !== undefined ? { upstreamFamily: op.upstreamFamily } : {}),
      ...(op.version !== undefined ? { version: op.version } : {}),
      ...(op.confidence !== undefined ? { confidence: op.confidence } : {}),
      ...(op.notes !== undefined ? { notes: op.notes } : {}),
      ...(op.package !== undefined ? { package: op.package } : {}),
      ...(op.subpath !== undefined ? { subpath: op.subpath } : {}),
      role: op.role ?? "module",
      ...(op.exportShape !== undefined ? { exportShape: op.exportShape } : {}),
      ...(op.fingerprint !== undefined ? { fingerprint: op.fingerprint } : {}),
    };
    annotations.set(chunkId, annotation);
    summaries.push({
      id: op.id,
      chunkId,
      chunkPath: op.chunkPath,
      identity: op.identity,
      level: op.level,
      ...(op.upstreamFamily !== undefined ? { upstreamFamily: op.upstreamFamily } : {}),
      ...(op.version !== undefined ? { version: op.version } : {}),
      ...(op.confidence !== undefined ? { confidence: op.confidence } : {}),
      ...(op.package !== undefined ? { package: op.package } : {}),
      ...(op.subpath !== undefined ? { subpath: op.subpath } : {}),
      role: op.role ?? "module",
    });
  }

  return {
    artifact,
    manifest: {
      kind: "js.vendor_annotations_manifest",
      counts: {
        annotations: annotations.size,
        considered: catalog.length,
        applied: ops.length,
      },
      annotations: summaries,
    },
  };
}

function validateOp(op) {
  for (const field of REQUIRED_FIELDS) {
    const value = op[field];
    if (value === undefined || value === null || value === "") {
      throw new Error(`mark_vendor operation ${op.id ?? "<unknown>"} is missing required field: ${field}`);
    }
  }
  if (op.operation !== "mark_vendor") {
    throw new Error(`mark_vendor operation ${op.id} has unexpected operation: ${op.operation}`);
  }
  if (!VALID_LEVELS.has(op.level)) {
    throw new Error(`mark_vendor operation ${op.id} has unknown level: ${op.level}`);
  }
  if (op.role !== undefined && op.role !== "module" && op.role !== "worker") {
    throw new Error(`mark_vendor operation ${op.id} has invalid role: ${op.role}`);
  }
  if (op.level === "swap") {
    for (const field of SWAP_REQUIRED) {
      const value = op[field];
      if (value === undefined || value === null || value === "") {
        throw new Error(`mark_vendor operation ${op.id} level "swap" is missing required field: ${field}`);
      }
    }
  }
  if (
    op.exportShape !== undefined &&
    (op.exportShape === null || typeof op.exportShape !== "object" || Array.isArray(op.exportShape))
  ) {
    throw new Error(`mark_vendor operation ${op.id} exportShape must be an object`);
  }
  if (op.fingerprint !== undefined) {
    const fp = op.fingerprint;
    if (!fp || typeof fp !== "object" || typeof fp.algorithm !== "string" || typeof fp.hash !== "string") {
      throw new Error(`mark_vendor operation ${op.id} fingerprint must have algorithm and hash strings`);
    }
  }
  if (!Array.isArray(op.evidence) || op.evidence.length === 0) {
    throw new Error(`mark_vendor operation ${op.id} requires a non-empty evidence array`);
  }
}

function chunkIdFromChunkPath(chunkPath, opId) {
  if (typeof chunkPath !== "string" || chunkPath === "") {
    throw new Error(`mark_vendor operation ${opId} has invalid chunkPath: ${chunkPath}`);
  }
  if (!chunkPath.endsWith(".js")) {
    throw new Error(`mark_vendor operation ${opId} chunkPath must end in .js: ${chunkPath}`);
  }
  return chunkPath.slice(0, -".js".length);
}
