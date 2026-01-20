-- Example: Reporting issues using direct SQL
-- Alternative to Python helpers. Use psql with credentials in PG* env vars.

-- Create issue header
INSERT INTO reported_issues (critic_run_id, issue_id, rationale)
VALUES (current_critic_run_id(), 'dead-code-utils-cleanup',
        'Function cleanup() in utils.py is never called');

-- Add occurrence (single file with line range)
INSERT INTO reported_issue_occurrences
  (critic_run_id, reported_issue_id, locations)
VALUES (current_critic_run_id(), 'dead-code-utils-cleanup',
        '[{"file": "src/utils.py", "start_line": 142, "end_line": 158}]'::jsonb);

-- Add occurrence (single file without line range)
INSERT INTO reported_issue_occurrences
  (critic_run_id, reported_issue_id, locations)
VALUES (current_critic_run_id(), 'unused-import-typing',
        '[{"file": "src/models.py"}]'::jsonb);

-- Add occurrence (multiple files - e.g., duplication)
INSERT INTO reported_issue_occurrences
  (critic_run_id, reported_issue_id, locations)
VALUES (current_critic_run_id(), 'duplicated-enum-status',
        '[{"file": "src/types.py", "start_line": 20, "end_line": 25},
          {"file": "src/persist.py", "start_line": 54, "end_line": 58}]'::jsonb);

-- Soft delete (correction) - use UPDATE, not DELETE
UPDATE reported_issues
SET cancelled_at = now(),
    cancellation_reason = 'False alarm - function is called via reflection'
WHERE issue_id = 'dead-code-utils-cleanup';

-- Submission must be done via MCP server - see report_issues.py.
