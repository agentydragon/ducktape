/**
 * Fixtures for the definition-detail scene: one agent definition's stats and per-example rows.
 */
export const definitionData = {
  image_digest: "sha256:abc123def456",
  agent_type: "critic",
  created_at: "2025-01-15T10:30:00Z",
  stats: {
    valid: {
      whole_snapshot: {
        recall_stats: { mean: 0.72, lower: 0.65, upper: 0.79 },
        n_examples: 45,
        zero_count: 3,
        status_counts: { completed: 42, timed_out: 3 },
        total_available: 50,
      },
      file_set: {
        recall_stats: { mean: 0.68, lower: 0.6, upper: 0.76 },
        n_examples: 30,
        zero_count: 5,
        status_counts: { completed: 28, timed_out: 2 },
        total_available: 35,
      },
    },
    train: {
      whole_snapshot: {
        recall_stats: { mean: 0.75, lower: 0.7, upper: 0.8 },
        n_examples: 100,
        zero_count: 8,
        status_counts: { completed: 95, timed_out: 5 },
        total_available: 120,
      },
      file_set: {
        recall_stats: { mean: 0.65, lower: 0.58, upper: 0.72 },
        n_examples: 80,
        zero_count: 12,
        status_counts: { completed: 75, timed_out: 5 },
        total_available: 100,
      },
    },
  },
  examples: [
    {
      snapshot_slug: "vuln-app-v1",
      example_kind: "whole_snapshot",
      files_hash: null,
      split: "valid",
      recall_denominator: 5,
      n_runs: 3,
      status_counts: { completed: 3 },
      credit_stats: { mean: 3.5, lower: 3.0, upper: 4.0 },
    },
    {
      snapshot_slug: "auth-service",
      example_kind: "file_set",
      files_hash: "abc123",
      split: "valid",
      recall_denominator: 3,
      n_runs: 2,
      status_counts: { completed: 2 },
      credit_stats: { mean: 2.0, lower: 1.5, upper: 2.5 },
    },
  ],
};
