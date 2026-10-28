import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import ts from "typescript";

const defaultDeclarations = readFileSync(new URL("./openapi_types_naming_default.d.ts", import.meta.url), "utf8");
const defaultSource = ts.createSourceFile(
  "openapi_types_naming_default.d.ts",
  defaultDeclarations,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TS
);
assert.equal(defaultSource.parseDiagnostics.length, 0);
const defaultRootTypeNames = defaultSource.statements
  .filter((statement) => ts.isTypeAliasDeclaration(statement))
  .map((statement) => statement.name.text);
assert.equal(defaultRootTypeNames.filter((name) => name === "HttpTrace").length, 1);
assert.equal(defaultRootTypeNames.includes("HTTPTrace"), false);

const keepCasingDeclarations = readFileSync(
  new URL("./openapi_types_naming_keep_casing.d.ts", import.meta.url),
  "utf8"
);
const keepCasingSource = ts.createSourceFile(
  "openapi_types_naming_keep_casing.d.ts",
  keepCasingDeclarations,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TS
);
assert.ok(keepCasingSource.parseDiagnostics.length > 0);
assert.ok(keepCasingDeclarations.includes("InvoiceLine-Input"));
