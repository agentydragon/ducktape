import { annotateProgram, parseModule, resolveIdentifiers } from "../translation/scope_metadata.mjs";

export function lowerDump(code, acorn, eslintScope) {
  return dumpParsed(parseModule(code, acorn.parse, eslintScope));
}

export function dumpParsed(parsed) {
  return annotateProgram(
    parsed,
    resolveIdentifiers(parsed, () => true)
  );
}
