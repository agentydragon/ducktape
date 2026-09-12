/**
 * Fixtures for the annotated-source scenes: a file, the ground truth over it, and a critic's
 * issues graded against that truth. Shared by both FileViewer scenes and, through
 * runs.ts's critic run, by the run-detail scene.
 */
export const fileContent = {
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
export const tps = [
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
export const fps = [
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
export const critiqueIssues = [
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
export const gradingEdges = [
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
