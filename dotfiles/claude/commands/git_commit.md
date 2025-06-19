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
1. Read the staged and unstaged diff (git diff HEAD).
2. Draft 1–5 candidate commits, each with:
   - A concise commit message.
   - A diffstat-style file list as shown above.
3. Do NOT modify the index while proposing; only inspect the diff.
4. Present all proposals in the above compact format.
5. Await user selection.
6. On selection, apply the chosen patch and commit in a single git command.
7. Loop until the working tree is clean or the user aborts.

Do NOT include tags (e.g., Signed-off-by) or ANSI color codes.
