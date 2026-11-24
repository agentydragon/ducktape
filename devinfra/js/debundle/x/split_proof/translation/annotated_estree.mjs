import { isDeepStrictEqual } from "node:util";
import { annotateProgram, parseModule, resolveIdentifiers } from "./scope_metadata.mjs";

export function lowerDump(code, acorn, eslintScope, options) {
  return dumpParsed(parseModule(code, acorn.parse, eslintScope), options);
}

export function dumpParsed(parsed, options) {
  return annotateProgram(
    parsed,
    resolveIdentifiers(parsed, () => true),
    options
  );
}

/** Validate a snapshot against its source before deriving the requested annotations. */
export function loadSnapshot(snapshot, acorn, eslintScope, options) {
  const legacy = snapshot == null || !Object.hasOwn(snapshot, "annotationVersion");
  if (!legacy && snapshot.annotationVersion !== 2)
    throw new Error(`unsupported annotationVersion: ${JSON.stringify(snapshot.annotationVersion)}`);
  const mismatch = legacy
    ? "legacy annotated ESTree disagrees with a fresh parse of sourceText"
    : "input annotations differ from a fresh parse of sourceText";
  if (typeof snapshot?.sourceText !== "string")
    throw new Error(legacy ? "expected an unversioned annotated ESTree Program with sourceText" : mismatch);
  const parsed = parseModule(snapshot.sourceText, acorn.parse, eslintScope);
  const meaning = resolveIdentifiers(parsed, () => true);
  if (!isDeepStrictEqual(annotateProgram(parsed, meaning, { legacy }), snapshot)) throw new Error(mismatch);
  return annotateProgram(parsed, meaning, options);
}
