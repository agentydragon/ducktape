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

/** Reconstruct printer metadata for an unversioned dump from the source that produced it. */
export function upgradeLowerDump(program, acorn, eslintScope, options) {
  if (!program || typeof program.sourceText !== "string" || Object.hasOwn(program, "annotationVersion"))
    throw new Error("expected an unversioned annotated ESTree Program with sourceText");
  const parsed = parseModule(program.sourceText, acorn.parse, eslintScope);
  const meaning = resolveIdentifiers(parsed, () => true);
  const legacy = annotateProgram(parsed, meaning, { legacy: true });
  if (!isDeepStrictEqual(legacy, program))
    throw new Error("legacy annotated ESTree disagrees with a fresh parse of sourceText");
  return annotateProgram(parsed, meaning, options);
}
