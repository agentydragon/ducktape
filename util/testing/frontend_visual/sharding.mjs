/**
 * Which scenes a Bazel shard of a visual test is responsible for.
 *
 * Shared by every multi-scene runner -- visual-test-lib.mjs's sweep and haku console's own
 * render.mjs -- because filter-then-shard is the same decision wherever scenes are batched, and
 * the Bazel protocol around it (advertise support, then read the shard environment) is easy to get
 * subtly wrong once, let alone twice.
 */
import { writeFileSync } from "node:fs";

/**
 * The subset of `names` this process should render, empty if the shard has nothing to do.
 *
 * `--test_filter` narrows the list, then the shard environment splits what remains. Filtering
 * first keeps a one-scene filter addressable whichever shard would otherwise own it: the match
 * runs wherever it lands and the other shards render nothing and pass.
 *
 * A caller that gets an empty list must not launch a browser for it -- there is nothing to render,
 * and a visual-review manifest with no assets is invalid.
 */
export function selectForShard(names) {
  // Bazel honours shard_count only for a runner that advertises support by touching this file,
  // and it must be touched whether or not this shard ends up owning any scene.
  const statusFile = process.env.TEST_SHARD_STATUS_FILE;
  if (statusFile) writeFileSync(statusFile, "");
  const filter = process.env.TESTBRIDGE_TEST_ONLY;
  const matched = filter ? names.filter((name) => name.includes(filter)) : names;
  if (filter && matched.length === 0) {
    throw new Error(`--test_filter=${filter} matched none of: ${names.join(", ")}`);
  }
  const total = Number(process.env.TEST_TOTAL_SHARDS ?? 1);
  const index = Number(process.env.TEST_SHARD_INDEX ?? 0);
  return matched.filter((_, position) => position % total === index);
}
