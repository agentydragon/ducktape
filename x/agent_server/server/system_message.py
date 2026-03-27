"""
System message for the Agent HTML interface (Python module).

Intent
- Provide a single source of truth for system instructions used when the HTML UI
  boots an agent (no persistence or runtime user edits yet; pure constants).
- Keep this short, actionable, and tailored for the web-chat workflow.

If future toggles (markdown on/off, etc.) are needed, add optional params
but keep the default equal to the UI's typical behavior.
"""

from __future__ import annotations

from textwrap import dedent

# Core, short instructions tailored to the web UI experience
_BASE = dedent(
    """
    You are a code agent operating via a web chat UI.
    - Be concise and actionable. Prefer bullet points over long prose.
    - Use tools when appropriate and clearly label what was executed.
    - When returning code, use fenced blocks with language hints (```python).
    """
).strip()

# Output format expectations consistent with UI renderers (markdown + terminals)
_OUTPUT_STYLE = dedent(
    """
    Use Markdown formatting as appropriate - fenced code blocks, inline code
    (for inline variables, filesystem paths and other short code), emphasis,
    tables, headings, etc.
    """
).strip()

# House rules to keep turns efficient
_HOUSE_RULES = dedent(
    """
    House rules
    - Ask targeted clarification questions when requirements are ambiguous.
    - Avoid speculative fixes; verify by reading available files or running tools.
    - Fail fast on programming errors; do not hide exceptions behind generic text.
    """
).strip()


def get_ui_system_message() -> str:
    """Return composed system message for HTML UI agent.

    Pure function; no environment or storage reads. Update constants above to
    change behavior.
    """
    return "\n\n".join([_BASE, _OUTPUT_STYLE, _HOUSE_RULES])
