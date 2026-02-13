// Page-level harness for visual regression testing
// Renders full pages with mock data to verify overall layout and navigation

import { mount } from "svelte";
import "../../src/app.css";

// Import page components
import DefinitionDetail from "../../src/components/DefinitionDetail.svelte";
import FileViewer from "../../src/components/FileViewer.svelte";
import LLMRequestViewer from "../../src/components/LLMRequestViewer.svelte";
import DistributionChart from "../../src/components/stats/DistributionChart.svelte";
import CoverageHeatmap from "../../src/components/stats/CoverageHeatmap.svelte";
import OccurrenceStats from "../../src/components/stats/OccurrenceStats.svelte";

// --- Mock Data for Pages ---

// Mock definition detail response
const mockDefinitionData = {
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

// Mock file content for FileViewer page test
const mockFileContent = {
  path: "src/auth/login.py",
  content: `"""User authentication module."""
import hashlib
import os

def hash_password(password: str) -> str:
    """Hash a password using MD5."""
    return hashlib.md5(password.encode()).hexdigest()

def verify_user(username: str, password: str) -> bool:
    """Verify user credentials."""
    # TODO: Add rate limiting
    stored_hash = get_stored_hash(username)
    if stored_hash is None:
        return False
    return stored_hash == hash_password(password)

def create_session(user_id: int) -> str:
    """Create a new session token."""
    token = os.urandom(16).hex()
    # Session expires in 24 hours
    store_session(user_id, token, expires=86400)
    return token`,
  line_count: 22,
};

// TPs: Real security issues
const mockTps = [
  {
    tp_id: "weak-hash-algorithm",
    rationale:
      "MD5 is cryptographically broken and should not be used for password hashing. Use bcrypt, scrypt, or Argon2 instead.",
    occurrences: [
      {
        occurrence_id: "occ-md5-usage",
        note: "Direct MD5 usage for password hashing",
        locations: [{ file: "src/auth/login.py", start_line: 5, end_line: 7, note: "MD5 hash function" }],
        critic_scopes_expected_to_recall: [["security", "cryptography"]],
      },
    ],
  },
];

// FPs: False positives
const mockFps = [
  {
    fp_id: "hardcoded-expiry",
    rationale:
      "The session expiry of 86400 seconds (24 hours) is a reasonable default and is clearly documented in the comment.",
    occurrences: [
      {
        occurrence_id: "occ-expiry-value",
        note: "This is a reasonable default, not a magic number",
        locations: [{ file: "src/auth/login.py", start_line: 19, end_line: 20 }],
        relevant_files: ["src/config/settings.py"],
      },
    ],
  },
];

// Critique issues from agent
const mockCritiqueIssues = [
  {
    issue_id: "critique-weak-crypto",
    rationale: "The code uses MD5 for password hashing which is insecure.",
    occurrences: [
      {
        occurrence_id: 1,
        note: "Found insecure hash algorithm",
        locations: [{ file: "src/auth/login.py", start_line: 5, end_line: 7 }],
      },
    ],
  },
  {
    issue_id: "critique-missing-rate-limit",
    rationale: "The verify_user function lacks rate limiting, enabling brute force attacks.",
    occurrences: [
      {
        occurrence_id: 2,
        note: "No rate limiting on login attempts",
        locations: [{ file: "src/auth/login.py", start_line: 9, end_line: 15 }],
      },
    ],
  },
];

// Grading edges
const mockGradingEdges = [
  {
    critique_issue_id: "critique-weak-crypto",
    target: {
      kind: "tp" as const,
      tp_id: "weak-hash-algorithm",
      occurrence_id: "occ-md5-usage",
      credit: 1.0,
    },
    rationale: "Correctly identified the MD5 weakness",
  },
  {
    critique_issue_id: "critique-missing-rate-limit",
    target: {
      kind: "fp" as const,
      fp_id: "rate-limit-false-positive",
      occurrence_id: "occ-rate-limit",
      credit: 0.0,
    },
    rationale: "Valid concern but marked as FP in ground truth",
  },
];

// Mock LLM requests
const mockLLMRequests = [
  {
    id: 1,
    model: "claude-sonnet-4-20250514",
    request_body: {
      model: "claude-sonnet-4-20250514",
      messages: [
        { role: "system", content: "You are a security code reviewer." },
        { role: "user", content: "Review this code for security issues:\n\n```python\nimport hashlib\n...\n```" },
      ],
      max_tokens: 4096,
    },
    response_body: {
      id: "msg_abc123",
      content: [
        {
          type: "text",
          text: "I found several security issues:\n\n1. **Weak hash algorithm**: MD5 is cryptographically broken...",
        },
      ],
      usage: { input_tokens: 250, output_tokens: 180 },
    },
    error: null,
    latency_ms: 2341,
    created_at: "2025-01-20T10:00:00Z",
  },
  {
    id: 2,
    model: "claude-sonnet-4-20250514",
    request_body: {
      model: "claude-sonnet-4-20250514",
      messages: [{ role: "user", content: "Can you elaborate on the rate limiting issue?" }],
    },
    response_body: {
      id: "msg_def456",
      content: [
        {
          type: "text",
          text: "The verify_user function should implement rate limiting to prevent brute force attacks...",
        },
      ],
      usage: { input_tokens: 100, output_tokens: 150 },
    },
    error: null,
    latency_ms: 1567,
    created_at: "2025-01-20T10:01:00Z",
  },
  {
    id: 3,
    model: "gpt-4o-mini",
    request_body: {
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "Summarize findings" }],
    },
    response_body: null,
    error: "Rate limit exceeded - retry after 30 seconds",
    latency_ms: 234,
    created_at: "2025-01-20T10:02:00Z",
  },
];

// Mock data for distribution charts
const mockRecallDistribution = Array.from({ length: 50 }, (_, i) => {
  // Create a realistic distribution: mostly 0.3-0.8 range with some outliers
  const base = 0.3 + Math.sin(i * 0.2) * 0.25 + (i / 50) * 0.2;
  return Math.max(0, Math.min(1, base + (i % 7) * 0.03));
});

const mockTpCountDistribution = [
  2, 3, 5, 5, 7, 8, 8, 9, 10, 10, 12, 12, 14, 15, 15, 18, 20, 22, 25, 28, 30, 35, 42, 50, 8, 6, 11, 13, 16, 19,
];

// Mock data for coverage heatmap
const mockCoverageDefinitions = [
  { image_digest: "sha256:aaa111bbb", best_on_count: 8, evaluated_on_count: 15 },
  { image_digest: "sha256:bbb222ccc", best_on_count: 6, evaluated_on_count: 14 },
  { image_digest: "sha256:ccc333ddd", best_on_count: 5, evaluated_on_count: 12 },
  { image_digest: "sha256:ddd444eee", best_on_count: 3, evaluated_on_count: 10 },
  { image_digest: "sha256:eee555fff", best_on_count: 2, evaluated_on_count: 8 },
];

const mockCoverageExamples = Array.from({ length: 20 }, (_, i) => ({
  snapshot_slug: `snapshot-${String.fromCharCode(65 + i)}`,
  example_kind: i % 3 === 0 ? "file_set" : "whole_snapshot",
  files_hash: i % 3 === 0 ? `hash${i}` : null,
  max_recall: 0.4 + (i % 5) * 0.12,
  tp_count: 3 + (i % 8),
}));

// Generate realistic cells: each definition evaluated on some subset of examples
const mockCoverageCells: Array<{ definition_idx: number; example_idx: number; recall: number; is_best: boolean }> = [];
for (let d = 0; d < mockCoverageDefinitions.length; d++) {
  for (let e = 0; e < mockCoverageExamples.length; e++) {
    // Not every definition evaluated on every example
    if ((d + e) % 3 === 0) continue; // skip ~1/3 for "not evaluated"
    const recall = Math.max(0, Math.min(1, 0.3 + d * 0.05 + e * 0.02 + Math.sin(d * e) * 0.15));
    const isBest = recall >= mockCoverageExamples[e].max_recall - 0.01;
    mockCoverageCells.push({ definition_idx: d, example_idx: e, recall, is_best: isBest });
  }
}

// Mock occurrence stats data
const mockOccurrenceStatsData = [
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

// --- Page Scenarios ---

const pages: Record<string, { component: any; props: Record<string, unknown> }> = {
  // Definition detail page - shows stats, CLI command, recall table
  DefinitionDetail: {
    component: DefinitionDetail,
    props: {
      data: mockDefinitionData,
    },
  },

  // File viewer with full annotations - TP, FP, critique issues, grading
  FileViewerAnnotated: {
    component: FileViewer,
    props: {
      file: mockFileContent,
      tps: mockTps,
      fps: mockFps,
      critiqueIssues: mockCritiqueIssues,
      gradingEdges: mockGradingEdges,
      snapshotSlug: "test-snapshot",
    },
  },

  // File viewer with just ground truth (no critique)
  FileViewerGroundTruth: {
    component: FileViewer,
    props: {
      file: mockFileContent,
      tps: mockTps,
      fps: mockFps,
      snapshotSlug: "test-snapshot",
    },
  },

  // LLM request viewer with multiple requests including errors (first request expanded)
  LLMRequests: {
    component: LLMRequestViewer,
    props: {
      requests: mockLLMRequests,
      initialExpanded: [1],
    },
  },

  // Distribution chart - recall histogram
  DistributionChartRecall: {
    component: DistributionChart,
    props: {
      values: mockRecallDistribution,
      title: "Max Recall Distribution (Valid Examples)",
      numBuckets: 10,
      valueFormat: (v: number) => `${(v * 100).toFixed(1)}%`,
      color: "rgb(59, 130, 246)",
    },
  },

  // Distribution chart - TP count histogram
  DistributionChartTP: {
    component: DistributionChart,
    props: {
      values: mockTpCountDistribution,
      title: "True Positive Count Distribution (Valid Examples)",
      numBuckets: 8,
      valueFormat: (v: number) => `${v.toFixed(0)}`,
      color: "rgb(34, 197, 94)",
    },
  },

  // Coverage heatmap
  CoverageHeatmap: {
    component: CoverageHeatmap,
    props: {
      definitions: mockCoverageDefinitions,
      examples: mockCoverageExamples,
      cells: mockCoverageCells,
    },
  },

  // Occurrence stats table
  OccurrenceStatsTable: {
    component: OccurrenceStats,
    props: {
      occurrences: mockOccurrenceStatsData,
    },
  },
};

// Parse URL parameters
const params = new URLSearchParams(window.location.search);
const pageName = params.get("page");

const app = document.getElementById("app")!;

if (!pageName) {
  // Show available pages
  app.innerHTML = `
    <div style="font-family: system-ui; padding: 20px;">
      <h1>Visual Test Harness</h1>
      <p>Available page scenarios:</p>
      <ul>
        ${Object.keys(pages)
          .map((name) => `<li><a href="?page=${name}">${name}</a></li>`)
          .join("")}
      </ul>
    </div>
  `;
} else if (!pages[pageName]) {
  app.innerHTML = `<div style="color: red; padding: 20px;">Unknown page: ${pageName}</div>`;
} else {
  const { component, props } = pages[pageName];
  mount(component, {
    target: app,
    props,
  });
}
