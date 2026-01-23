// Page-level harness for visual regression testing
// Renders full pages with mock data to verify overall layout and navigation

import { mount } from "svelte";
import "../../src/app.css";

// Import page components
import DefinitionDetail from "../../src/components/DefinitionDetail.svelte";
import FileViewer from "../../src/components/FileViewer.svelte";
import LLMRequestViewer from "../../src/components/LLMRequestViewer.svelte";

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
        status_counts: { completed: 42, max_turns_exceeded: 3 },
        total_available: 50,
      },
      file_set: {
        recall_stats: { mean: 0.68, lower: 0.6, upper: 0.76 },
        n_examples: 30,
        zero_count: 5,
        status_counts: { completed: 28, max_turns_exceeded: 2 },
        total_available: 35,
      },
    },
    train: {
      whole_snapshot: {
        recall_stats: { mean: 0.75, lower: 0.7, upper: 0.8 },
        n_examples: 100,
        zero_count: 8,
        status_counts: { completed: 95, max_turns_exceeded: 5 },
        total_available: 120,
      },
      file_set: {
        recall_stats: { mean: 0.65, lower: 0.58, upper: 0.72 },
        n_examples: 80,
        zero_count: 12,
        status_counts: { completed: 75, max_turns_exceeded: 5 },
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
  line_count: 21,
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
        files: [
          {
            path: "src/auth/login.py",
            ranges: [{ start_line: 5, end_line: 7, note: "MD5 hash function" }],
          },
        ],
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
        files: [
          {
            path: "src/auth/login.py",
            ranges: [{ start_line: 19, end_line: 20 }],
          },
        ],
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
        occurrence_id: "critique-occ-1",
        note: "Found insecure hash algorithm",
        files: [
          {
            path: "src/auth/login.py",
            ranges: [{ start_line: 5, end_line: 7 }],
          },
        ],
      },
    ],
  },
  {
    issue_id: "critique-missing-rate-limit",
    rationale: "The verify_user function lacks rate limiting, enabling brute force attacks.",
    occurrences: [
      {
        occurrence_id: "critique-occ-2",
        note: "No rate limiting on login attempts",
        files: [
          {
            path: "src/auth/login.py",
            ranges: [{ start_line: 9, end_line: 15 }],
          },
        ],
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

  // LLM request viewer with multiple requests including errors
  LLMRequests: {
    component: LLMRequestViewer,
    props: {
      requests: mockLLMRequests,
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
