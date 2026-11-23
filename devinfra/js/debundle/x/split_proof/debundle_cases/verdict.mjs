export const rejectKind = (expected) =>
  "atomic_conflict" in expected.reject ? "atomic_conflict" : "cycle" in expected.reject ? "cycle" : "stderr";

/** What the emitted entry should print. The adapter renames nothing, so it is the original's output. */
export const wanted = (expected) => expected.accept.original_stdout ?? expected.accept.stdout;

/**
 * `agree-*`: same verdict. `strict-*`: I reject what debundle accepts (`runs` when my plan, written anyway, prints
 * the expected output; `breaks` when it does not, so debundle's splitter is better than mine). `unsound-accept`: I
 * accept and the emitted modules print something else. `lenient`: I accept what debundle rejects (the output is the
 * same on this run). `refusal-test`: the case checks debundle's error text, which has no counterpart here.
 */
export function classify(expected, mine, node) {
  for (const [label, observation] of Object.entries(node)) {
    if (observation.failure) return `error:${label}-${observation.failure}`;
  }
  if ("accept" in expected) {
    if (node.original.code !== 0 || node.original.stdout !== wanted(expected)) return "error:original-baseline";
    const same = node.emitted.code === 0 && node.emitted.stdout === wanted(expected);
    if (mine.verdict === "accept") return same ? "agree-accept" : "unsound-accept";
    return same ? "strict-but-runs" : "strict-and-breaks";
  }
  if (rejectKind(expected) === "stderr") return "refusal-test";
  if (mine.verdict === "reject") return "agree-reject";
  return node.emitted.code === node.original.code && node.emitted.stdout === node.original.stdout
    ? "lenient"
    : "unsound-accept";
}
