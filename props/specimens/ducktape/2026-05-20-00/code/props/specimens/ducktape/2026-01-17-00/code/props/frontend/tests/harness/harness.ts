// Component harness for visual regression testing
// Mounts components based on URL parameters

import { mount } from "svelte";
import "../../src/app.css";

// Import components for testing
import BackButton from "../../src/components/BackButton.svelte";
import Breadcrumb from "../../src/components/Breadcrumb.svelte";
import CopyButton from "../../src/components/CopyButton.svelte";
import FileViewer from "../../src/components/FileViewer.svelte";
import FileTree from "../../src/components/FileTree.svelte";
import IssueComment from "../../src/components/IssueComment.svelte";

// Mock data for FileViewer tests - a Python file with TPs, FPs, and critique issues
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

// TPs: Real security issues that should be found
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

// FPs: Things that look like issues but are actually fine
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

// Critique issues: What a critic agent might report
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

// Grading edges: How critique issues map to ground truth
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
      kind: "tp" as const,
      tp_id: "missing-rate-limit",
      occurrence_id: "occ-rate-limit",
      credit: 0.0, // No matching TP in this snapshot
    },
    rationale: "Valid concern but not in ground truth for this example",
  },
];

// Mock file tree for FileTree tests
const mockFileTree = [
  {
    name: "src",
    path: "src",
    is_dir: true,
    tp_count: 3,
    fp_count: 1,
    children: [
      {
        name: "auth",
        path: "src/auth",
        is_dir: true,
        tp_count: 2,
        fp_count: 1,
        children: [
          { name: "login.py", path: "src/auth/login.py", is_dir: false, tp_count: 1, fp_count: 1, children: [] },
          { name: "session.py", path: "src/auth/session.py", is_dir: false, tp_count: 1, fp_count: 0, children: [] },
        ],
      },
      {
        name: "utils",
        path: "src/utils",
        is_dir: true,
        tp_count: 1,
        fp_count: 0,
        children: [
          { name: "crypto.py", path: "src/utils/crypto.py", is_dir: false, tp_count: 1, fp_count: 0, children: [] },
          { name: "helpers.py", path: "src/utils/helpers.py", is_dir: false, tp_count: 0, fp_count: 0, children: [] },
        ],
      },
    ],
  },
  { name: "README.md", path: "README.md", is_dir: false, tp_count: 0, fp_count: 0, children: [] },
  { name: "setup.py", path: "setup.py", is_dir: false, tp_count: 0, fp_count: 0, children: [] },
];

// Mock grading edges for IssueComment tests
const mockIssueCommentGradingEdges = [
  {
    critique_issue_id: "critique-1",
    target: { kind: "tp" as const, tp_id: "tp-1", occurrence_id: "occ-1", credit: 1.0 },
    rationale: "Correctly identified the security vulnerability",
  },
  {
    critique_issue_id: "critique-1",
    target: { kind: "tp" as const, tp_id: "tp-2", occurrence_id: "occ-2", credit: 0.5 },
    rationale: "Partially correct - missed some details",
  },
];

// Component registry with their test scenarios
const components: Record<string, { component: any; scenarios: Record<string, Record<string, unknown>> }> = {
  BackButton: {
    component: BackButton,
    scenarios: {
      Default: {},
      CustomLabel: { label: "← Go Back" },
      CustomHref: { href: "/custom-path", label: "← Return" },
      CustomClass: { class: "text-lg text-blue-600 hover:text-blue-800 font-semibold", label: "← Styled Back" },
    },
  },
  Breadcrumb: {
    component: Breadcrumb,
    scenarios: {
      SingleItem: { items: [{ label: "snapshot-name" }] },
      WithPath: {
        items: [
          { label: "snapshot-name", href: "/snapshots/snapshot-name" },
          { label: "src" },
          { label: "components" },
          { label: "Button.tsx" },
        ],
      },
      DeepPath: {
        items: [
          { label: "snapshot-name", href: "/snapshots/snapshot-name" },
          { label: "src" },
          { label: "features" },
          { label: "auth" },
          { label: "components" },
          { label: "LoginForm.tsx" },
        ],
      },
      AllLinked: {
        items: [
          { label: "Home", href: "/" },
          { label: "Snapshots", href: "/snapshots" },
          { label: "snapshot-1", href: "/snapshots/snapshot-1" },
          { label: "file.py" },
        ],
      },
    },
  },
  CopyButton: {
    component: CopyButton,
    scenarios: {
      Default: { text: "https://example.com/some/url/to/copy" },
      CustomLabel: { text: "console.log('Hello, World!');", label: "Copy Code" },
      CustomSuccessMessage: {
        text: "git clone https://github.com/example/repo.git",
        label: "Copy",
        successMessage: "Git command copied!",
      },
      LongText: {
        text: "https://example.com/very/long/url/that/might/need/to/be/copied/for/deep/linking/purposes",
        label: "Copy URL",
      },
    },
  },
  FileViewer: {
    component: FileViewer,
    scenarios: {
      WithTpAndFp: {
        file: mockFileContent,
        tps: mockTps,
        fps: mockFps,
        snapshotSlug: "test-snapshot",
      },
      WithCritiqueIssues: {
        file: mockFileContent,
        tps: mockTps,
        fps: mockFps,
        critiqueIssues: mockCritiqueIssues,
        gradingEdges: mockGradingEdges,
        snapshotSlug: "test-snapshot",
      },
      TpOnly: {
        file: mockFileContent,
        tps: mockTps,
        fps: [],
        snapshotSlug: "test-snapshot",
      },
      Empty: {
        file: { path: "empty.py", content: "# Empty file\n", line_count: 1 },
        tps: [],
        fps: [],
      },
    },
  },
  FileTree: {
    component: FileTree,
    scenarios: {
      Default: {
        nodes: mockFileTree,
        onFileClick: () => {},
      },
      WithSelection: {
        nodes: mockFileTree,
        onFileClick: () => {},
        selectedPath: "src/auth/login.py",
      },
    },
  },
  IssueComment: {
    component: IssueComment,
    scenarios: {
      TpCollapsed: {
        kind: "tp",
        issueId: "weak-hash-algorithm/occ-md5-usage",
        rationale: "MD5 is cryptographically broken and should not be used for password hashing.",
        note: "Direct MD5 usage for password hashing",
        expanded: false,
      },
      TpExpanded: {
        kind: "tp",
        issueId: "weak-hash-algorithm/occ-md5-usage",
        rationale:
          "MD5 is cryptographically broken and should not be used for password hashing. Use bcrypt, scrypt, or Argon2 instead.",
        note: "Direct MD5 usage for password hashing",
        allFiles: [
          { path: "src/auth/login.py", ranges: [{ start_line: 5, end_line: 7 }] },
          { path: "src/utils/crypto.py", ranges: [{ start_line: 10, end_line: 12 }] },
        ],
        expanded: true,
      },
      FpCollapsed: {
        kind: "fp",
        issueId: "hardcoded-expiry/occ-expiry-value",
        rationale: "The session expiry of 86400 seconds is a reasonable default.",
        expanded: false,
      },
      FpExpanded: {
        kind: "fp",
        issueId: "hardcoded-expiry/occ-expiry-value",
        rationale:
          "The session expiry of 86400 seconds (24 hours) is a reasonable default and is clearly documented in the comment.",
        note: "This is a reasonable default, not a magic number",
        allFiles: [{ path: "src/auth/login.py", ranges: [{ start_line: 19, end_line: 20 }] }],
        expanded: true,
      },
      CritiqueCollapsed: {
        kind: "critique",
        issueId: "critique-1",
        rationale: "The code uses MD5 for password hashing which is insecure.",
        expanded: false,
      },
      CritiqueExpanded: {
        kind: "critique",
        issueId: "critique-1",
        rationale:
          "The code uses MD5 for password hashing which is insecure. This allows attackers to use rainbow tables or brute force to crack passwords.",
        note: "Found insecure hash algorithm",
        allFiles: [{ path: "src/auth/login.py", ranges: [{ start_line: 5, end_line: 7 }] }],
        gradingEdges: mockIssueCommentGradingEdges,
        snapshotSlug: "test-snapshot",
        expanded: true,
      },
    },
  },
};

// Parse URL parameters
const params = new URLSearchParams(window.location.search);
const componentName = params.get("component");
const scenarioName = params.get("scenario") || "Default";

const app = document.getElementById("app")!;

if (!componentName) {
  // Show available components and scenarios
  app.innerHTML = `
    <div style="font-family: system-ui; padding: 20px;">
      <h1>Component Harness</h1>
      <p>Available components and scenarios:</p>
      <ul>
        ${Object.entries(components)
          .map(
            ([name, { scenarios }]) => `
          <li>
            <strong>${name}</strong>
            <ul>
              ${Object.keys(scenarios)
                .map((s) => `<li><a href="?component=${name}&scenario=${s}">${s}</a></li>`)
                .join("")}
            </ul>
          </li>
        `
          )
          .join("")}
      </ul>
    </div>
  `;
} else if (!components[componentName]) {
  app.innerHTML = `<div style="color: red;">Unknown component: ${componentName}</div>`;
} else {
  const { component, scenarios } = components[componentName];
  const props = scenarios[scenarioName];

  if (!props) {
    app.innerHTML = `<div style="color: red;">Unknown scenario: ${scenarioName} for ${componentName}</div>`;
  } else {
    // Mount the component with props
    mount(component, {
      target: app,
      props,
    });
  }
}
