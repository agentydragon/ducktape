import assert from "node:assert/strict";
import { createHash } from "node:crypto";

export const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

// Source Map v3 deltas. Source/original/name state carries across generated
// lines; generated columns reset at each semicolon. A one-field segment
// terminates the previous source's coverage.
function fields(segment) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  const out = [];
  let value = 0;
  let factor = 1;
  for (const char of segment) {
    const digit = alphabet.indexOf(char);
    assert(digit >= 0, "invalid source map digit");
    value += (digit & 31) * factor;
    assert(Number.isSafeInteger(value), "source map VLQ overflow");
    if (digit & 32) {
      factor *= 32;
    } else {
      out.push((value & 1 ? -1 : 1) * Math.floor(value / 2));
      value = 0;
      factor = 1;
    }
  }
  assert.equal(factor, 1, "incomplete source map VLQ");
  assert([1, 4, 5].includes(out.length), "invalid source map segment");
  return out;
}

function positionOffsets(code) {
  const offsets = [0];
  for (const match of code.matchAll(/\r\n|[\n\r\u2028\u2029]/g)) offsets.push(match.index + match[0].length);
  return offsets;
}

function lineRanges(code, offsets) {
  return offsets.map((start, line) => {
    const length = code.slice(start, offsets[line + 1] ?? code.length).replace(/[\r\n\u2028\u2029]+$/, "").length;
    return { start, length };
  });
}

export function mapIntervals(code, map) {
  assert.equal(map.version, 3);
  assert(!map.sourceRoot && !map.sections, "expected a flat source map without sourceRoot");
  if (map.sourcesContent) assert.equal(map.sources.length, map.sourcesContent.length);
  const generated = lineRanges(code, positionOffsets(code));
  const originals = map.sourcesContent?.map((s) => lineRanges(s, positionOffsets(s)));
  let source = 0;
  let originalLine = 0;
  let originalColumn = 0;
  return map.mappings.split(";").flatMap((line, i) => {
    if (!line) return [];
    const { start, length } = generated[i];
    let column = 0;
    const points = line.split(",").map((segment, j) => {
      const [dc, ds, dl, do_] = fields(segment);
      assert(j === 0 ? dc >= 0 : dc > 0, "generated columns not strictly increasing");
      column += dc;
      assert(column <= length, "generated column outside line");
      if (ds === undefined) return { column, source: null };
      source += ds;
      originalLine += dl;
      originalColumn += do_;
      assert(map.sources[source] && originalLine >= 0 && originalColumn >= 0);
      if (originals) assert(originalColumn <= originals[source][originalLine]?.length, "original column outside line");
      return { column, source: map.sources[source], originalLine, originalColumn };
    });
    return points.map((point, j) => ({
      start: start + point.column,
      end: start + (points[j + 1]?.column ?? length),
      source: point.source,
      originalLine: point.originalLine,
      originalColumn: point.originalColumn,
    }));
  });
}

// Acorn and source maps use UTF-16; bundle pins and byte counts use UTF-8.
export function byteOffsets(code) {
  const offsets = new Map([[0, 0]]);
  let utf16 = 0;
  let bytes = 0;
  for (const char of code) {
    utf16 += char.length;
    bytes += Buffer.byteLength(char);
    offsets.set(utf16, bytes);
  }
  return (index) => {
    assert(offsets.has(index), "offset splits a Unicode code point");
    return offsets.get(index);
  };
}

export function classifyInput(library, file) {
  if (file === library + ".mjs") return { file: "entry:" + file, level: "fixture" };
  const match = /(?:^|\/)node_modules\/((?:acorn|lodash-es|semver|zod)\/.+)$/.exec(file);
  assert(match, `unexpected build input: ${file}`);
  const name = match[1];
  // These JS files are distribution artifacts. Do not infer TS paths by
  // filename or guess Acorn authoring files from statement names.
  const level = name.startsWith("acorn/") || name.startsWith("zod/") ? "distribution" : "source_file";
  return { file: name, level };
}

export function composeAcorn(dist, artifact) {
  assert.equal(sha256(dist), artifact.distSha256, "Acorn distribution changed");
  const offsets = positionOffsets(dist);
  const intervals = mapIntervals(dist, artifact.map);
  return ({ originalLine, originalColumn }) => {
    const position = offsets[originalLine] + originalColumn;
    let low = 0;
    let high = intervals.length;
    while (low < high) {
      const mid = Math.floor((low + high) / 2);
      if (intervals[mid].start <= position) low = mid + 1;
      else high = mid;
    }
    const candidate = intervals[low - 1];
    return candidate?.end > position ? candidate.source : null;
  };
}

export function attribute(library, code, units, map, metafile, sourceHome = () => null) {
  const outputs = Object.values(metafile.outputs).filter(({ entryPoint }) => entryPoint);
  assert.equal(outputs.length, 1);
  const [output] = outputs;
  assert.equal(output.entryPoint, library + ".mjs");
  assert.equal(output.bytes, Buffer.byteLength(code));
  const inputFiles = new Map(
    Object.entries(output.inputs).map(([name, { bytesInOutput }]) => {
      assert(Object.hasOwn(metafile.inputs, name), `missing metafile input: ${name}`);
      return [name, { ...classifyInput(library, name), bytesInOutput }];
    })
  );
  assert.equal(new Set(Array.from(inputFiles.values(), ({ file }) => file)).size, inputFiles.size);
  for (const name of map.sources) assert(inputFiles.has(name), `mapped file absent from metafile: ${name}`);

  const intervals = mapIntervals(code, map);
  const toByte = byteOffsets(code);
  // In this pinned esbuild bundle, synthetic namespace shells can have
  // mappings back to export syntax. They are not authored implementation.
  const namespaceObjects = new Set(
    library === "zod"
      ? units.flatMap(({ node }) => {
          const call = node.type === "ExpressionStatement" && node.expression;
          return call?.type === "CallExpression" &&
            call.callee.name === "__export" &&
            call.arguments[0]?.type === "Identifier" &&
            call.arguments[1]?.type === "ObjectExpression"
            ? [call.arguments[0].name]
            : [];
        })
      : []
  );
  let next = 0;
  let previousEnd = 0;
  const rows = units.map(({ start, end, node }, ordinal) => {
    assert(previousEnd <= start && start < end);
    previousEnd = end;
    const startByte = toByte(start);
    const endByte = toByte(end);
    while (intervals[next]?.start < start) next++;
    // Require a mapping anchor inside the unit. In particular don't let
    // whitespace or a split declarator inherit a neighbouring unit's owner.
    const hits = new Map();
    const originalHits = new Map();
    let unresolvedMappedBytes = 0;
    for (let i = next; intervals[i]?.start < end; i++) {
      const point = intervals[i];
      if (point.source === null) continue;
      const input = inputFiles.get(point.source);
      const mappedBytes = toByte(Math.min(end, point.end)) - toByte(point.start);
      hits.set(input, (hits.get(input) ?? 0) + mappedBytes);
      const home = input.level === "source_file" ? input.file : sourceHome(input, point);
      if (home !== null) originalHits.set(home, (originalHits.get(home) ?? 0) + mappedBytes);
      else if (input.level !== "fixture") unresolvedMappedBytes += mappedBytes;
    }
    const homes = Array.from(hits, ([{ file, level }, mappedBytes]) => ({ file, level, mappedBytes }));
    const mappedBytes = homes.reduce((sum, row) => sum + row.mappedBytes, 0);
    assert(mappedBytes <= endByte - startByte);
    const generatedExport = ordinal === units.length - 1 && node.type === "ExportNamedDeclaration" && !node.declaration;
    const generatedPrelude =
      homes.length === 0 &&
      (library === "zod" || library === "semver") &&
      node.type === "VariableDeclaration" &&
      node.declarations.every(({ id }) => id.type === "Identifier" && id.name.startsWith("__")) &&
      rowsBeforeFirstInput(code, start, inputFiles);
    const originals = Array.from(originalHits, ([file, mappedBytes]) => ({ file, mappedBytes }));
    const generatedNamespaceCall =
      node.type === "ExpressionStatement" &&
      node.expression.type === "CallExpression" &&
      node.expression.callee.name === "__export" &&
      namespaceObjects.has(node.expression.arguments[0]?.name);
    const generatedNamespaceObject =
      node.type === "VariableDeclaration" &&
      node.declarations.length > 0 &&
      node.declarations.every(
        ({ id, init }) =>
          namespaceObjects.has(id.name) && init?.type === "ObjectExpression" && init.properties.length === 0
      );
    const classification =
      generatedExport || generatedPrelude || generatedNamespaceCall || generatedNamespaceObject
        ? "generated"
        : homes.length === 0
          ? "unmapped"
          : homes.length > 1 || originals.length > 1
            ? "mixed"
            : originals.length === 1
              ? "source_file"
              : homes[0].level;
    return {
      ordinal,
      start,
      end,
      startByte,
      endByte,
      type: node.type,
      classification,
      inputs: homes,
      originals,
      mappedBytes,
      unresolvedMappedBytes,
    };
  });
  return { rows, inputs: Array.from(inputFiles.values()).sort((a, b) => a.file.localeCompare(b.file, "en")) };
}

function rowsBeforeFirstInput(code, start, inputFiles) {
  // Readable esbuild writes a section comment before the first package input.
  // Only classify prelude helpers before that boundary; similar names elsewhere
  // do not establish provenance.
  const first = code.indexOf("\n// ");
  return (
    first >= 0 &&
    start < first &&
    Array.from(inputFiles.keys()).some((file) => code.startsWith("// " + file + "\n", first + 1))
  );
}
