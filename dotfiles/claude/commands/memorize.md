# /memorize Command

When you see `/memorize` in a user prompt, it means the user wants you to remember something important and update appropriate documentation.

## What to do:

1. **Identify what to memorize** - Usually refers to the preceding content or technique just demonstrated
2. **Determine scope** - Is this a global preference or project-specific?
   - Look for context clues about scope
   - When unclear, default to project-specific unless it's clearly a universal preference
3. **Update appropriate documentation**:
   - **Global preferences** → `~/.claude/CLAUDE.md`
     - Tool preferences (e.g., "always use rg instead of grep")
     - Universal coding standards (e.g., "use XDG spec for configs")
     - General workflow preferences
   - **Project-specific** → `./CLAUDE.md` or relevant project docs
     - Project conventions
     - Technical discoveries about the specific codebase
     - API/protocol findings
   - **Code patterns** → Add comments in source files
4. **Confirm the update** - Tell the user what was memorized and where

## Examples:

**User:** "cool how you just did thing foobar /memorize it"
- Add the "foobar" technique to appropriate CLAUDE.md
- Response: "Memorized the foobar technique in CLAUDE.md under [section]"

**User:** "that checksum field doesn't actually exist /memorize"
- Update technical documentation removing checksum references
- Add clarifying comments in code
- Response: "Documented that checksum fields don't exist in docs/UNKNOWN.md and types.ts"

**User:** "always use ripgrep instead of grep /memorize"
- Add to global CLAUDE.md coding standards
- Response: "Added to CLAUDE.md: Always use ripgrep (rg) instead of grep"

## How to determine scope:

**Global** (goes in `~/.claude/CLAUDE.md`):
- Universal tool preferences ("always use rg not grep")
- General coding standards ("use XDG spec")
- Editor/workflow preferences
- Security practices
- Performance guidelines that apply everywhere

**Project-specific** (goes in `./CLAUDE.md` or project docs):
- "In this codebase, use TranslogBuilder not raw JSON"
- "This API doesn't actually have checksum fields"
- Project-specific patterns or conventions
- Discoveries about the specific system being analyzed

**Clues for scope:**
- "always" → likely global
- "in this project/codebase" → project-specific
- Technical discoveries → usually project-specific
- Tool preferences → usually global
- When unclear → ask or default to project-specific

## Important:

- Be specific about WHERE you documented it
- Quote the exact text you added when possible
- If updating multiple files, list all of them
- Consider if it belongs in global vs project-specific docs
