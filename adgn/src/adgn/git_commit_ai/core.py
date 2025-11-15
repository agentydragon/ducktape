from __future__ import annotations

import re

import pygit2

# Shared constants used by backends and CLI
MAX_PROMPT_CONTEXT_BYTES = 100 * 1024  # 100 KiB cap for AI context block
PAST_COMMITS_MAX_CHARS = 6000
RECENT_COMMITS_FOR_CONTEXT = 30
DIFF_SNIPPET_CHARS = 5000


def _len_bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _cap_append(
    parts: list[str],
    chunk: str,
    cap_bytes: int,
    truncation_note: str,
) -> bool:
    """Append chunk to parts unless this would exceed cap; returns True if truncated."""
    current_bytes = _len_bytes("".join(parts))
    needed_bytes = _len_bytes(chunk)
    if current_bytes + needed_bytes >= cap_bytes:
        remaining_bytes = cap_bytes - current_bytes
        if remaining_bytes > 0:
            parts.append(
                chunk.encode("utf-8")[:remaining_bytes].decode(
                    "utf-8",
                    errors="ignore",
                ),
            )
        parts.append(truncation_note + "\n")
        return True
    parts.append(chunk)
    return False


def _head_commit_oid(repo: pygit2.Repository) -> pygit2.Oid:
    commit = repo.revparse_single("HEAD").peel(pygit2.Commit)
    return commit.id


def _index_tree_oid(repo: pygit2.Repository) -> pygit2.Oid:
    return repo.index.write_tree()


def _diff(repo: pygit2.Repository, include_all: bool) -> pygit2.Diff:
    a = _head_commit_oid(repo)
    if include_all:
        # Compare HEAD vs working tree
        return repo.diff(a, None, cached=False)
    # Compare HEAD vs index (staged)
    return repo.diff(a, None, cached=True)


def _format_name_status(repo: pygit2.Repository, include_all: bool) -> str:
    diff = _diff(repo, include_all)
    lines: list[str] = []
    for d in diff.deltas:
        st = d.status
        if st == pygit2.GIT_DELTA_ADDED:
            lines.append(f"A\t{d.new_file.path}")
        elif st == pygit2.GIT_DELTA_DELETED:
            lines.append(f"D\t{d.old_file.path}")
        elif st == pygit2.GIT_DELTA_MODIFIED:
            lines.append(f"M\t{d.new_file.path}")
        elif st == pygit2.GIT_DELTA_RENAMED:
            lines.append(f"R\t{d.old_file.path}\t{d.new_file.path}")
        elif st == pygit2.GIT_DELTA_TYPECHANGE:
            lines.append(f"T\t{d.new_file.path}")
        else:
            # Other statuses (e.g., copied, ignored) — skip for prompt brevity
            continue
    return "\n".join(lines)


def _format_unified_diff(repo: pygit2.Repository, include_all: bool) -> str:
    return _diff(repo, include_all).patch or ""


def include_all_from_passthru(passthru: list[str]) -> bool:
    """Return True if '-a' or '--all' flags are present."""
    return ("-a" in passthru) or ("--all" in passthru)


# Unified mapping for status letters to human text (commit template rendering)
STATUS_LETTER_TO_TEXT: dict[str, str] = {
    "A": "new file:",
    "M": "modified:",
    "D": "deleted:",
    "R": "renamed:",
    "T": "typechange:",
}


def status_letter_text(letter: str) -> str:
    return STATUS_LETTER_TO_TEXT.get(letter, f"{letter}:")


def _format_status_porcelain(repo: pygit2.Repository) -> str:
    """Return a minimal porcelain-like status using pygit2 flags.

    Prints lines of the form 'XY path' and '?? path' for untracked.
    """
    out: list[str] = []
    st = repo.status()
    for path, flags in st.items():
        x = " "
        y = " "
        # Untracked
        if flags & pygit2.GIT_STATUS_WT_NEW and not (
            flags
            & (
                pygit2.GIT_STATUS_INDEX_NEW
                | pygit2.GIT_STATUS_INDEX_MODIFIED
                | pygit2.GIT_STATUS_INDEX_DELETED
                | pygit2.GIT_STATUS_INDEX_RENAMED
                | pygit2.GIT_STATUS_INDEX_TYPECHANGE
            )
        ):
            out.append(f"?? {path}")
            continue
        # Index (X)
        if flags & pygit2.GIT_STATUS_INDEX_NEW:
            x = "A"
        elif flags & pygit2.GIT_STATUS_INDEX_MODIFIED:
            x = "M"
        elif flags & pygit2.GIT_STATUS_INDEX_DELETED:
            x = "D"
        elif flags & pygit2.GIT_STATUS_INDEX_RENAMED:
            x = "R"
        elif flags & pygit2.GIT_STATUS_INDEX_TYPECHANGE:
            x = "T"
        # Worktree (Y)
        if flags & pygit2.GIT_STATUS_WT_MODIFIED:
            y = "M"
        elif flags & pygit2.GIT_STATUS_WT_DELETED:
            y = "D"
        elif flags & pygit2.GIT_STATUS_WT_TYPECHANGE:
            y = "T"
        out.append(f"{x}{y} {path}")
    return "\n".join(out)


def _log_subjects(repo: pygit2.Repository, n: int = 10) -> list[str]:
    """Return up to n commit subjects following the first-parent chain.

    This avoids SortMode typing mismatches across pygit2 versions while still
    producing a sensible, linear history for commit-context prompts.
    """
    head = repo.revparse_single("HEAD").peel(pygit2.Commit)
    out: list[str] = []
    cur = head
    i = 0
    while True:
        msg = cur.message or ""
        subj = msg.splitlines()[0] if msg else ""
        out.append(subj)
        i += 1
        if i >= n or not cur.parents:
            break
        # Follow first-parent to keep a linear chain
        cur = cur.parents[0]
    return out


def _build_ai_context(repo: pygit2.Repository, include_all: bool) -> str:
    parts: list[str] = []

    parts.append("$ git status --porcelain\n")
    status_out = _format_status_porcelain(repo) + "\n"
    _cap_append(parts, status_out, MAX_PROMPT_CONTEXT_BYTES, "[Context truncated to 100 KiB]")

    ns_header = "git diff HEAD --name-status" if include_all else "git diff --cached --name-status"
    parts.append(f"$ {ns_header}\n")
    ns_out = _format_name_status(repo, include_all) + "\n"
    _cap_append(parts, ns_out, MAX_PROMPT_CONTEXT_BYTES, "[Context truncated to 100 KiB]")

    parts.append(
        f"$ git log --no-color -n {RECENT_COMMITS_FOR_CONTEXT} --stat --pretty=format:%h %s\n",
    )
    log_out = "\n".join(_log_subjects(repo, RECENT_COMMITS_FOR_CONTEXT)) + "\n"
    _cap_append(parts, log_out, MAX_PROMPT_CONTEXT_BYTES, "[Context truncated to 100 KiB]")

    diff_header = "git diff HEAD --unified=0" if include_all else "git diff --cached --unified=0"
    parts.append(f"$ {diff_header}\n")
    diff_out = _format_unified_diff(repo, include_all) + "\n"
    _cap_append(parts, diff_out, MAX_PROMPT_CONTEXT_BYTES, "[Context truncated to 100 KiB]")

    out = "".join(parts)
    if _len_bytes(out) > MAX_PROMPT_CONTEXT_BYTES:
        out = out.encode("utf-8")[:MAX_PROMPT_CONTEXT_BYTES].decode("utf-8", errors="ignore")
        out += "\n[Context truncated to 100 KiB]\n"
    return out


def diffstat(repo: pygit2.Repository, passthru: list[str]) -> str:
    include_all = include_all_from_passthru(passthru)
    diff = _diff(repo, include_all)
    # Emulate a minimal --stat: path and change status
    lines: list[str] = []
    for d in diff.deltas:
        st = d.status
        if st == pygit2.GIT_DELTA_ADDED:
            lines.append(f"{d.new_file.path} | A")
        elif st == pygit2.GIT_DELTA_DELETED:
            lines.append(f"{d.old_file.path} | D")
        elif st == pygit2.GIT_DELTA_MODIFIED:
            lines.append(f"{d.new_file.path} | M")
        elif st == pygit2.GIT_DELTA_RENAMED:
            lines.append(f"{d.old_file.path} -> {d.new_file.path} | R")
        elif st == pygit2.GIT_DELTA_TYPECHANGE:
            lines.append(f"{d.new_file.path} | T")
    return "\n".join(lines)


def build_prompt(
    repo: pygit2.Repository,
    diff: str,
    passthru: list[str],
    previous_message: str | None = None,
) -> str:
    include_all = include_all_from_passthru(passthru)
    context = _build_ai_context(repo, include_all)
    if previous_message:
        prompt = f"""Update and refine this existing commit message based on the current changes.

Previous commit message:
{previous_message}

The commit is being amended. Write an updated message that accurately reflects all changes.
Output ONLY the commit message between <message> and </message> tags.
No explanations, no markdown, no signatures. Do NOT include 'Generated with' or 'Co-Authored-By' lines.

Context:
{context}
"""
    else:
        prompt = f"""Write a concise, imperative-mood Git commit message.
Output ONLY the commit message between <message> and </message> tags.
No explanations, no markdown, no signatures. Do NOT include 'Generated with' or 'Co-Authored-By' lines.

Context:
{context}

Example outputs:
<message>
Add user authentication to API endpoints
</message>

<message>
Refactor database connection handling

- Extract connection pool logic into separate module
- Add retry mechanism for transient failures
</message>

Diffstat:
$ {"git diff HEAD --stat" if include_all else "git diff --cached --stat"}

{diffstat(repo, passthru)}
"""
    if len(diff) < DIFF_SNIPPET_CHARS:
        prompt = (
            prompt
            + f"""
Staged diff:

{diff}"""
        )
    else:
        prompt = (
            prompt
            + f"""
Staged diff (first to {DIFF_SNIPPET_CHARS} of {len(diff)} chars)

{diff[:DIFF_SNIPPET_CHARS]}"""
        )
    # Past commits (subjects only)
    # Append past commit subjects
    log_subjects = _log_subjects(repo, 10)
    for i, subj in enumerate(log_subjects):
        new_prompt = prompt
        if i == 0:
            new_prompt += """\n\nPast commits (subjects):\n\n"""
        new_prompt += f"- {subj}\n"
        if len(new_prompt) > PAST_COMMITS_MAX_CHARS:
            break
        prompt = new_prompt
    return prompt


def _extract_message_from_text(text: str) -> str:
    if match := re.search(r"<message>\s*(.*?)\s*</message>", text, re.DOTALL):
        return match.group(1).strip()
    return text.strip()
