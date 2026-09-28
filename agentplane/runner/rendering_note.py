"""What agentplane tells every harness about how its own responses render, independent of
whatever standing instructions the operator set for the session."""

# Keep in sync with the languages agentplane/app/frontend/syntax_highlight.tsx registers.
_HIGHLIGHTED_LANGUAGES = "bash, javascript, json, python, typescript, and yaml"

RENDERING_NOTE = (
    "Your responses render as GitHub-Flavored Markdown in a web UI. Fenced code blocks are "
    f"syntax-highlighted when tagged with one of these languages: {_HIGHLIGHTED_LANGUAGES}. Tag "
    "a fenced code block with its language whenever the content is one of those."
)


def with_rendering_note(instructions: str) -> str:
    """`instructions` (the session's own standing instructions, possibly empty) with the rendering
    note placed ahead of them, as one string a harness takes as a single instructions/system-prompt
    value."""
    return f"{RENDERING_NOTE}\n\n{instructions}" if instructions else RENDERING_NOTE
