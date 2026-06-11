# 2025-07-12

`CLAUDE-2025-07-12.md`: grew extremely long. stopping.

what's good:

- "breadcrumb-examples"

what was added:

- "never replace unknowns with placeholders" (e.g. unicode char -> '?')
- no redundant documentation
- rg > grep, comby > manual, ast-grep > regex, use jscpd for dedupe, AST for code
- never disable warnings, lints, tests, checks etc
- never build on unverified assumptions
- do exactly what was asked and not more
- claude code settings (CLAUDE.md, .mcp.json, ...)
- ducktape -> ansible, dotfiles, llm, ... (what it is & why)
- messy workspace -> organize (md file sprawl etc)
- save full context for future claude
- "never commit unless explicitly asked"
- TodoWrite: every task, mark todos, etc.

never got to work:

- llm similar (semantic search)
- learnings file

## Task

- Task tool suspends you until all agents finish. to parallelize, launch multiple
  in parallel.

- how to Task - avoid stepping on each others' toes, prefer:
  - not modify files
  - read-only tasks
  - isolated work areas (folders)
  - read-only tasks

- tasks can abort halfway/abruptly, there's no mechanism to report partial progress
