import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export const DEFAULT_PARSER_PLUGINS = Object.freeze(["jsx", "typescript", "importAssertions", "topLevelAwait"]);

export const DEFAULT_PARSER_OPTIONS = Object.freeze({
  allowUndeclaredExports: true,
  plugins: DEFAULT_PARSER_PLUGINS,
  sourceType: "module",
});

export const MODULE_PACKAGE_JSON = Object.freeze({ type: "module" });

export function cloneDefaultParserOptions() {
  return {
    ...DEFAULT_PARSER_OPTIONS,
    plugins: [...DEFAULT_PARSER_PLUGINS],
  };
}

export function modulePackageJson() {
  return { ...MODULE_PACKAGE_JSON };
}

export function writeTextFile(path, text) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, text);
}

export function writeJsonFile(path, value) {
  writeTextFile(path, `${JSON.stringify(value, null, 2)}\n`);
}
