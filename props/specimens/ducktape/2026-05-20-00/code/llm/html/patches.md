# Patches

If I'm asking you for help editing some text file (say a long piece of code) and you
are showing me what to edit where, present your edits:

- In a fenced Markdown code block
- Formatted as an _executable Linux command_ like `patch` or `apply`

Do not apply this on binary files, obviously.

Example:

## Creating a file

When I ask you to write a _whole program_ and contextually it sounds like I want
a new whole file, do something like this:

```bash
cat <<'EOF' > greet.py
#!/usr/bin/env python3

"""Simple greeting script"""

import sys


def greet(name: str) -> None:
    print(f"Hello, {name}!")


if __name__ == "__main__":
    greet(sys.argv[1] if len(sys.argv) > 1 else "World")
EOF
```

## Patching

For patches, **use standard unified diff format**:

```bash
patch -p0 <<'PATCH'
--- greet.py
+++ greet.py
@@
-"""Simple greeting script"""
+"""Simple greeting script (v1.1)"""

@@
-    print(f"Hello, {name}!")
+    print(f"Greetings, {name}! 👋")
PATCH
```

NOTE: **`patch` expects a real diff header (`---` / `+++` …)** and would choke
on any decorative `*** Start Patch` lines! DO NOT add those!
