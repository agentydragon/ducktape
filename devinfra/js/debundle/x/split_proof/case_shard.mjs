import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";

export function caseShard() {
  const count = Number(process.env.TEST_TOTAL_SHARDS ?? 1);
  const index = Number(process.env.TEST_SHARD_INDEX ?? 0);
  assert(Number.isInteger(count) && count > 0, "invalid TEST_TOTAL_SHARDS");
  assert(Number.isInteger(index) && index >= 0 && index < count, "invalid TEST_SHARD_INDEX");
  if (process.env.TEST_SHARD_STATUS_FILE) writeFileSync(process.env.TEST_SHARD_STATUS_FILE, "");

  // Every shard visits the same case names in the same order.
  const names = new Set();
  return (name) => {
    assert(!names.has(name), `duplicate case ${name}`);
    const selected = names.size % count === index;
    names.add(name);
    if (selected) console.log(`Case ${name}`);
    return selected;
  };
}
