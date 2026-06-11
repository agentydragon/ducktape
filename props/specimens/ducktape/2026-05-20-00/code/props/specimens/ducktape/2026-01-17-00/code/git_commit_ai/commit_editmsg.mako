<%doc>
Editor prefill template for git-commit-ai.
The commented section gets '# ' prefix via Mako filter; verbose diff below scissors is verbatim.
</%doc>
<%!
import textwrap
import pygit2

MAX_VERBOSE_DIFF_LINES = 3000

# Status flags that indicate a file is staged (not purely untracked)
_STAGED_FLAGS = (
    pygit2.GIT_STATUS_INDEX_NEW
    | pygit2.GIT_STATUS_INDEX_MODIFIED
    | pygit2.GIT_STATUS_INDEX_DELETED
    | pygit2.GIT_STATUS_INDEX_RENAMED
    | pygit2.GIT_STATUS_INDEX_TYPECHANGE
)

STATUS_TO_TEXT = {
    pygit2.GIT_DELTA_ADDED: "new file:",
    pygit2.GIT_DELTA_MODIFIED: "modified:",
    pygit2.GIT_DELTA_DELETED: "deleted:",
    pygit2.GIT_DELTA_RENAMED: "renamed:",
    pygit2.GIT_DELTA_TYPECHANGE: "typechange:",
}

def comment_prefix(text):
    return textwrap.indent(text, "# ", lambda line: True)

def delta_path(delta):
    return delta.old_file.path if delta.status == pygit2.GIT_DELTA_DELETED else delta.new_file.path

def delta_status_text(delta):
    return STATUS_TO_TEXT.get(delta.status, "unknown:")

%>
<%
# Compute view data from repo
branch = repo.head.shorthand if not repo.head_is_detached else "HEAD detached"
staged_diff = repo.diff(repo.head.target, None, cached=True)
unstaged_diff = repo.diff()
untracked_files = [
    path for path, flags in repo.status().items()
    if (flags & pygit2.GIT_STATUS_WT_NEW) and not (flags & _STAGED_FLAGS)
]

# Verbose: use flag or fall back to git config commit.verbose
if verbose:
    include_verbose = True
else:
    try:
        cfg_val = repo.config["commit.verbose"]
        include_verbose = cfg_val.strip().lower() in {"1", "true", "yes", "on"}
    except KeyError:
        include_verbose = False
%>\
<%def name="verbose_diff_lines(diff)">\
<%
    patch = diff.patch or ""
    lines = patch.splitlines()
    total = len(lines)
%>\
% for i, line in enumerate(lines):
% if i >= MAX_VERBOSE_DIFF_LINES:
[TRUNCATED: showing first ${MAX_VERBOSE_DIFF_LINES} of ${total} lines]
<% break %>\
% endif
${line}
% endfor
</%def>\
<%def name="commented()" filter="comment_prefix">\
% if user_context:
User context (-m):
${user_context}

% endif
% if previous_message:
Previous commit message (being amended):
${previous_message}

% endif
ai-draft${"(cached)" if cached else ""}: ${f"{elapsed_s:.2f}"}s

Please enter the commit message for your changes. Lines starting
with '#' will be ignored, and an empty message aborts the commit.

On branch ${branch}

% if list(staged_diff.deltas):
Changes to be committed:
% for delta in staged_diff.deltas:
	${delta_status_text(delta).ljust(12)} ${delta_path(delta)}
% endfor
% endif
% if list(unstaged_diff.deltas):

Changes not staged for commit:
  ("git add <file>..." to update what will be committed)
  ("git restore <file>..." to discard changes in working directory)
% for delta in unstaged_diff.deltas:
	${delta_status_text(delta).ljust(12)} ${delta_path(delta)}
% endfor
% endif
% if untracked_files:

Untracked files:
  ("git add <file>..." to include in what will be committed)
% for filename in untracked_files:
	${filename}
% endfor
% endif

${scissors_mark}
Do not modify or remove the line above.
Everything below it will be ignored.
</%def>\
${commented()}\
% if include_verbose:
${verbose_diff_lines(staged_diff)}\
% endif
