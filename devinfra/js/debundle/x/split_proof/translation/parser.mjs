import { isDeepStrictEqual } from "node:util";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "./annotated_estree.mjs";
import { annotateProgram, parseModule, resolveIdentifiers } from "./scope_metadata.mjs";

export { acorn, eslintScope };

export function dump(source, options) {
  return dumpParsed(parseModule(source, acorn.parse, eslintScope), options);
}

/** Validate a snapshot against its source before deriving the requested annotations. */
export function loadSnapshot(snapshot, options) {
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
