import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { split } from "./split.mjs";

const [requestPath] = process.argv.slice(2);
assert(requestPath, "usage: split_cli <request.json>");
const request = JSON.parse(readFileSync(requestPath, "utf8"));
const { includeLogs = false, ...options } = request;
const result = split(options);
const response = {
  status: result.status,
  scope: result.scope,
  guarantee: result.guarantee,
  reason: result.reason,
  reasonCode: result.reasonCode,
  files: result.files,
};
if (includeLogs) {
  response.stdout = result.stdout;
  response.stderr = result.stderr;
}
process.stdout.write(`${JSON.stringify(response)}\n`);
