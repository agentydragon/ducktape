# Dissolve

## Description
Progressively eliminate a file by redistributing its valuable content to appropriate locations and removing redundant, deprecated, or duplicate information. The goal is to either completely remove the file or reduce it to an essential kernel that cannot yet be eliminated.

## Instructions

### 1. Analyze the Target File
- Read the entire file to understand its content
- Identify the file's purpose and why it might be marked for dissolution
- Check if it's documentation, code, config, or other type

### 2. Scan for Existing Locations
- Search the codebase for files with similar content
- Identify established documentation structure
- Find logical homes for different sections
- Look for duplicate or overlapping information

### 3. Categorize Content
For each section/piece of information, classify it as:
- **Duplicate**: Already exists elsewhere (note where)
- **Outdated**: Deprecated or no longer accurate
- **Misplaced**: Belongs in another file (identify target)
- **Unique**: Not found elsewhere, still valuable
- **Essential**: Must remain (e.g., backward compatibility)

### 4. Progressive Dissolution
Execute in this order:
1. **Remove duplicates** - Delete content that exists elsewhere
2. **Remove deprecated** - Delete outdated/obsolete information
3. **Move misplaced** - Transfer content to appropriate files
4. **Consolidate unique** - Merge similar unique content
5. **Document essentials** - Explain why remaining content must stay

### 5. Actions to Take
- **For duplicates**: Delete and add comment pointing to canonical location
- **For deprecated**: Delete entirely (unless historical value)
- **For misplaced**:
  - Copy to appropriate file
  - Update/improve if needed
  - Delete from source
- **For unique valuable content**:
  - Find or create appropriate home
  - Move with proper context
- **For essential remnants**:
  - Add clear comment explaining why it remains
  - Consider renaming file to reflect reduced scope

### 6. Final Steps
- If file is empty: Delete it
- If file has essential remnants:
  - Rename to reflect actual purpose
  - Add header explaining why it still exists
  - Document migration in commit message
- Update any references to the dissolved file
- Create a summary of what was moved where

## Example Workflow

```bash
# Analyzing SUMMARY.md for dissolution
# Found:
# - Quick start section → duplicate of docs/guides/quick-start.md
# - Architecture overview → belongs in docs/architecture/README.md
# - Old TODO items → outdated, can remove
# - Unique insights → move to docs/findings/insights.md
# - File can be completely removed after redistribution
```

## Principles
- Always preserve valuable information
- Improve organization while moving content
- Document the dissolution process
- Ensure no broken references
- Prefer existing homes over creating new files
- Be aggressive about removing true duplicates
- Be conservative about deleting unique content

## Common Dissolution Targets
- README files that grew too large
- Old planning/summary documents
- Scattered documentation files
- Deprecated guides or instructions
- Temporary analysis files
- Old TODO or notes files

## Output Format
Provide a dissolution report:
```
DISSOLUTION REPORT: [filename]

DUPLICATES REMOVED:
- Section X → already in file.md
- Section Y → exists in other.md

CONTENT MOVED:
- Architecture details → docs/architecture/system.md
- API examples → docs/examples/api-usage.md

DEPRECATED REMOVED:
- Old setup instructions (superseded by new guide)
- Outdated limitations (no longer apply)

REMAINING CONTENT:
- [If any] Why it must remain

RESULT: [File deleted | File reduced to X lines | File renamed to Y]
```
