/**
 * Fixtures for the stats scenes: recall and TP-count distributions, the coverage heatmap's
 * definitions/examples/cells, and per-occurrence credit rows.
 */
export const recallDistribution = Array.from({ length: 50 }, (_, i) => {
  // Create a realistic distribution: mostly 0.3-0.8 range with some outliers
  const base = 0.3 + Math.sin(i * 0.2) * 0.25 + (i / 50) * 0.2;
  return Math.max(0, Math.min(1, base + (i % 7) * 0.03));
});

export const tpCountDistribution = [
  2, 3, 5, 5, 7, 8, 8, 9, 10, 10, 12, 12, 14, 15, 15, 18, 20, 22, 25, 28, 30, 35, 42, 50, 8, 6, 11, 13, 16, 19,
];

export const coverageDefinitions = [
  { image_digest: "sha256:aaa111bbb", best_on_count: 8, evaluated_on_count: 15 },
  { image_digest: "sha256:bbb222ccc", best_on_count: 6, evaluated_on_count: 14 },
  { image_digest: "sha256:ccc333ddd", best_on_count: 5, evaluated_on_count: 12 },
  { image_digest: "sha256:ddd444eee", best_on_count: 3, evaluated_on_count: 10 },
  { image_digest: "sha256:eee555fff", best_on_count: 2, evaluated_on_count: 8 },
];

export const coverageExamples = Array.from({ length: 20 }, (_, i) => ({
  snapshot_slug: `snapshot-${String.fromCharCode(65 + i)}`,
  example_kind: i % 3 === 0 ? "file_set" : "whole_snapshot",
  files_hash: i % 3 === 0 ? `hash${i}` : null,
  max_recall: 0.4 + (i % 5) * 0.12,
  tp_count: 3 + (i % 8),
}));

// Generate realistic cells: each definition evaluated on some subset of examples
export const coverageCells: Array<{ definition_idx: number; example_idx: number; recall: number; is_best: boolean }> =
  [];
for (let d = 0; d < coverageDefinitions.length; d++) {
  for (let e = 0; e < coverageExamples.length; e++) {
    // Not every definition evaluated on every example
    if ((d + e) % 3 === 0) continue; // skip ~1/3 for "not evaluated"
    const recall = Math.max(0, Math.min(1, 0.3 + d * 0.05 + e * 0.02 + Math.sin(d * e) * 0.15));
    const isBest = recall >= coverageExamples[e].max_recall - 0.01;
    coverageCells.push({ definition_idx: d, example_idx: e, recall, is_best: isBest });
  }
}

export const occurrenceStats = [
  {
    snapshot_slug: "vuln-app-v1",
    split: "valid",
    tp_id: "weak-hash-algorithm",
    occurrence_id: "occ-md5-usage",
    n_runs: 15,
    mean_credit: 0.85,
    min_credit: 0.5,
    max_credit: 1.0,
  },
  {
    snapshot_slug: "vuln-app-v1",
    split: "valid",
    tp_id: "sql-injection",
    occurrence_id: "occ-login-query",
    n_runs: 12,
    mean_credit: 0.42,
    min_credit: 0.0,
    max_credit: 1.0,
  },
  {
    snapshot_slug: "vuln-app-v1",
    split: "valid",
    tp_id: "missing-auth-check",
    occurrence_id: "occ-admin-panel",
    n_runs: 10,
    mean_credit: 0.15,
    min_credit: 0.0,
    max_credit: 0.5,
  },
  {
    snapshot_slug: "auth-service",
    split: "train",
    tp_id: "xss-reflected",
    occurrence_id: "occ-search-param",
    n_runs: 8,
    mean_credit: 0.92,
    min_credit: 0.7,
    max_credit: 1.0,
  },
  {
    snapshot_slug: "auth-service",
    split: "train",
    tp_id: "path-traversal",
    occurrence_id: "occ-file-download",
    n_runs: 6,
    mean_credit: 0.0,
    min_credit: 0.0,
    max_credit: 0.0,
  },
];
