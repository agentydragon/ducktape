import { annotateProgram, resolveIdentifiers } from "./scope_metadata.mjs";

export function dumpParsed(parsed, options) {
  return annotateProgram(
    parsed,
    resolveIdentifiers(parsed, () => true),
    options
  );
}
