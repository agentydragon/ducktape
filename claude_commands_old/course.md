Course correct when assumptions are unverified or false.

When invoked, systematically address incorrect or unverified assumptions to prevent future occurrences.

## Phase 1: Identify the Assumption

1. **Clarify what's being assumed**:
   - What specific assumption was made?
   - Is it unverified (might be true but wasn't checked) or false (definitely wrong)?
   - Where did this assumption come from?

2. **Get on the same page**:
   - Confirm the exact assumption issue
   - Understand why it's problematic
   - Identify what should have been done instead

## Phase 2: Create Corrective Actions (using TodoWrite)

### Todo 1: Update documentation

- Add to CLAUDE.md (local and/or global):
  - "Don't assume [specific thing]"
  - How to verify this assumption
  - What to do if verification isn't possible
  - Example of the bad assumption and correction

### Todo 2: Fix the source

- Find where the incorrect information originated:
  - Documentation that's wrong/incomplete
  - Comments that are misleading
  - Code that implies incorrect behavior
- Fix by:
  - Deleting if completely false
  - Marking as "UNVERIFIED: [assumption]"
  - Marking as "FALSE: [what's actually true]"
  - Adding verification steps

### Todo 3: Find and fix all instances

- **Spawn parallel agents** to:
  - Search for similar assumptions in:
    - Code comments
    - Documentation
    - Variable/function names
    - Error messages
  - For each instance:
    - Add corrective comment
    - Update to reflect reality
    - Add pointers to verification methods

## Phase 3: Implement Verification Culture

- Document how to verify this type of assumption
- Add checks/tests where possible
- Create patterns that force verification (e.g., required parameters instead of defaults)

## Example Usage

### Example 1: Unverified assumption

U: /course you're assuming this API returns JSON but you never checked
A: You're right - I assumed the API returns JSON without verifying. Is this:

- Unverified (might return JSON but we should check Content-Type header)
- False (definitely returns something else like XML/plaintext)?

U: unverified - could be JSON or error page HTML

### Example 2: False assumption

U: /course wrong - you assumed all node IDs start with '-' but system nodes don't
A: I see - I falsely assumed all node IDs have a '-' prefix. Let me correct this systematically.

[Creates todos to update docs about node ID formats, fix the source of this misinformation, and search for other places making this assumption]

### Example 3: Vague correction needed

U: /course this auth flow doesn't work like you think
A: What specifically am I assuming wrong about the auth flow?

- Token refresh mechanism?
- Required headers?
- Authentication endpoint?
- Token expiration behavior?

U: you're assuming refresh tokens expire in 1 hour but they actually last 30 days
