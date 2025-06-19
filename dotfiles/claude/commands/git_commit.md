# Git Commit

Generate and apply git commits by splitting the current diff into logical hunks.
Proposals must follow this format (plain text, no ANSI codes, minimal whitespace):
Commit 1: <Commit message>
 <file1> | <n> <+->
 <file2> | <n> <+->
 ...
 <X> files changed, <Y> insertions(+), <Z> deletions(-)
Ask: "Which commit would you like to apply? (1–N or n to abort)"

Instructions:
1. Check for both tracked changes (git diff HEAD) and untracked files (git ls-files --others --exclude-standard).
2. Draft 1–5 candidate commits, each with:
   - A concise commit message.
   - A diffstat-style file list as shown above.
   - IMPORTANT: Each commit must have a single, clear purpose. Never mix unrelated files.
   - Group related changes (e.g., feature + its tests) but split unrelated items.
   - Skip temporary/test files (e.g., foo, test.txt, .tmp).
   - For untracked files, show them with "A" (added) in the file list.
   - Consider whether untracked files belong with existing changes or as separate commits.
3. Do NOT modify the index while proposing; only inspect the diff and file list.
4. Present all proposals in the above compact format.
5. Await user selection.
6. On selection, apply the chosen patch and commit in a single git command.
7. Loop until the working tree is clean or the user aborts.

Do NOT include tags (e.g., Signed-off-by) or ANSI color codes.
