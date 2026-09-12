/**
 * Fixtures for the run scenes: the runs browser's listing, and one critic run detailed enough
 * for the run-detail view — which reuses the issues and grading edges from files.ts rather
 * than restating them, so the two scenes agree about what the critic found.
 */
import { critiqueIssues, gradingEdges } from "./files";

export const runs = [
  {
    agent_run_id: "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    image_digest: "sha256:abc123def456789012345678901234567890123456789012345678901234",
    type_config: {
      agent_type: "critic" as const,
      example: { kind: "whole_snapshot" as const, snapshot_slug: "vuln-app-v1", files_hash: null },
    },
    model: "gpt-5.1-codex-mini",
    status: "exited" as const,
    created_at: "2025-01-20T10:00:00Z",
    updated_at: "2025-01-20T10:05:00Z",
    split: "valid" as const,
    reported_issues_count: 3,
    grading: { tp_count: 2, fp_count: 1, total_credit: 1.5 },
  },
  {
    agent_run_id: "b2c3d4e5-f6a7-8901-bcde-f12345678901",
    image_digest: "sha256:abc123def456789012345678901234567890123456789012345678901234",
    type_config: {
      agent_type: "critic" as const,
      example: { kind: "file_set" as const, snapshot_slug: "auth-service", files_hash: "abc123" },
    },
    model: "gpt-5.1",
    status: "in_progress" as const,
    created_at: "2025-01-20T11:00:00Z",
    updated_at: "2025-01-20T11:00:30Z",
    split: "train" as const,
    reported_issues_count: null,
    grading: null,
  },
  {
    agent_run_id: "c3d4e5f6-a7b8-9012-cdef-123456789012",
    image_digest: "sha256:bbb222ccc333ddd444eee555fff666aaa111bbb222ccc333ddd444eee555f",
    type_config: { agent_type: "grader" as const, snapshot_slug: "vuln-app-v1" },
    model: "gpt-5.1-codex-mini",
    status: "exited" as const,
    created_at: "2025-01-20T10:10:00Z",
    updated_at: "2025-01-20T10:12:00Z",
    split: "valid" as const,
    reported_issues_count: null,
    grading: null,
  },
  {
    agent_run_id: "d4e5f6a7-b8c9-0123-defa-234567890123",
    image_digest: "sha256:abc123def456789012345678901234567890123456789012345678901234",
    type_config: {
      agent_type: "critic" as const,
      example: { kind: "whole_snapshot" as const, snapshot_slug: "auth-service", files_hash: null },
    },
    model: "gpt-5.1-codex-mini",
    status: "timed_out" as const,
    created_at: "2025-01-19T08:00:00Z",
    updated_at: "2025-01-19T09:00:00Z",
    split: "valid" as const,
    reported_issues_count: 5,
    grading: { tp_count: 3, fp_count: 0, total_credit: 2.75 },
  },
  {
    agent_run_id: "e5f6a7b8-c9d0-1234-efab-345678901234",
    image_digest: "sha256:ccc333ddd444eee555fff666aaa111bbb222ccc333ddd444eee555fff666a",
    type_config: { agent_type: "critic_dev_optimize" as const },
    model: "gpt-5.1",
    status: "exited" as const,
    created_at: "2025-01-18T14:00:00Z",
    updated_at: "2025-01-18T15:30:00Z",
    split: null,
    reported_issues_count: null,
    grading: null,
  },
];

export const criticRunDetail = {
  agent_run_id: "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  image_digest: "sha256:abc123def456789012345678901234567890123456789012345678901234",
  model: "gpt-5.1-codex-mini",
  status: "exited" as const,
  created_at: "2025-01-20T10:00:00Z",
  updated_at: "2025-01-20T10:05:00Z",
  llm_call_count: 5,
  budget_usd: 0.5,
  parent_agent_run_id: null,
  llm_costs: {
    totals: { requests: 5, input_tokens: 2000, cached_tokens: 500, output_tokens: 800, cost_usd: 0.0342 },
  },
  type_config: {
    agent_type: "critic" as const,
    example: { kind: "whole_snapshot" as const, snapshot_slug: "vuln-app-v1", files_hash: null },
  },
  details: {
    agent_type: "critic" as const,
    reported_issues: critiqueIssues,
    resolved_files: ["src/auth/login.py"],
    grader_runs: [
      {
        agent_run_id: "f6a7b8c9-d0e1-2345-abcd-678901234567",
        agent_type: "grader" as const,
        grading_edges: gradingEdges,
      },
    ],
  },
  child_runs: [{ agent_run_id: "f6a7b8c9-d0e1-2345-abcd-678901234567", agent_type: "grader" as const }],
};
