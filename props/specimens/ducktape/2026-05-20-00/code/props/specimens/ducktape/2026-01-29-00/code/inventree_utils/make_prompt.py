import textwrap
from pathlib import Path

import pyperclip

root = Path()
prompt = "Here's my current codebase for maintaining and beautifying my Inventree instance:\n\n"
prompt += "\n\n".join(
    [
        f"### {p.relative_to(root)}\n\n" + textwrap.indent(p.read_text(), "  ")
        for p in sorted(root.iterdir())
        if p.name
        not in ("inventree_config.yaml", ".git", "make_prompt.py", "labels", "__pycache__", "samplebooks_import")
    ]
)
prompt += textwrap.dedent(
    """

        -------

        I want you to make some changes to this codebase.
        In your output I want either:
          (a) for every file you're updating or deleting, the name and
              *full content* of the file -- *not* just the parts you're
              editing, OR
          (b) a copy-pasteable command I can put into my terminal that applies
              the changes you want to apply - e.g., like a call to `apply`.

        Do not use blanket "try - except Exception" or "try - except:" blocks.
        Just let things crash. Don't hide potential programming errors.
        If you do want to catch and exception, catch only the specific
        exceptions you actually want to target.

        ### Task

        """
)

print(prompt)
pyperclip.copy(prompt)
