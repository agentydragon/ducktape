/**
 * Fixtures for the snapshot-detail scene: one snapshot's ground truth and its file tree.
 */
export const snapshotDetail = {
  slug: "vuln-app-v1",
  split: "valid" as const,
  created_at: "2025-01-15T10:30:00Z",
  true_positives: [
    {
      tp_id: "weak-hash-algorithm",
      rationale: "MD5 is cryptographically broken and should not be used for password hashing.",
      occurrences: [
        {
          occurrence_id: "occ-md5-usage",
          note: "Direct MD5 usage for password hashing",
          locations: [{ file: "src/auth/login.py", start_line: 5, end_line: 7 }],
          critic_scopes_expected_to_recall: [["security", "cryptography"]],
        },
      ],
    },
    {
      tp_id: "sql-injection",
      rationale: "User input directly interpolated into SQL query without parameterization.",
      occurrences: [
        {
          occurrence_id: "occ-login-query",
          note: "String formatting in SQL query",
          locations: [{ file: "src/db/queries.py", start_line: 12, end_line: 15 }],
          critic_scopes_expected_to_recall: [["security", "injection"]],
        },
      ],
    },
  ],
  false_positives: [
    {
      fp_id: "hardcoded-expiry",
      rationale: "Session expiry of 86400 seconds is a reasonable default.",
      occurrences: [
        {
          occurrence_id: "occ-expiry-value",
          note: "Reasonable default, not a magic number",
          locations: [{ file: "src/auth/login.py", start_line: 19, end_line: 20 }],
          relevant_files: ["src/config/settings.py"],
        },
      ],
    },
  ],
};

export const fileTree = {
  tree: [
    {
      path: "src",
      name: "src",
      is_dir: true,
      tp_count: 2,
      fp_count: 1,
      children: [
        {
          path: "src/auth",
          name: "auth",
          is_dir: true,
          tp_count: 1,
          fp_count: 1,
          children: [
            { path: "src/auth/login.py", name: "login.py", is_dir: false, tp_count: 1, fp_count: 1, children: null },
            {
              path: "src/auth/session.py",
              name: "session.py",
              is_dir: false,
              tp_count: 0,
              fp_count: 0,
              children: null,
            },
          ],
        },
        {
          path: "src/db",
          name: "db",
          is_dir: true,
          tp_count: 1,
          fp_count: 0,
          children: [
            { path: "src/db/queries.py", name: "queries.py", is_dir: false, tp_count: 1, fp_count: 0, children: null },
          ],
        },
      ],
    },
    { path: "README.md", name: "README.md", is_dir: false, tp_count: 0, fp_count: 0, children: null },
  ],
};
