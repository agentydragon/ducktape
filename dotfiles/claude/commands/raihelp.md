List all available custom commands from global and project configurations.

When invoked, immediately scan and display all available slash commands in a concise CLI help format.

## Implementation

When called:

1. List files in `~/.claude/commands/*.md`
2. List files in `./.claude/commands/*.md` (if directory exists)
3. For each command file:
   - Extract name from filename (remove .md extension)
   - Read first line of file content as description
4. Display in compact CLI format

The output should be concise and scannable, similar to standard CLI tool help messages.
