"""Prompt constants for the Grocy MCP eval.

Per-case task prompts live in `cases.py`; what stays here is the voice and
the postmortem, which we share across every case so reflections are
comparable between rollouts.
"""

SYSTEM_PROMPT = "You are an inventory management assistant using Grocy."

POSTMORTEM_PROMPT = """\
Now that you've completed the task, please reflect on your experience using \
the Grocy MCP tools:

1. Was there anything confusing about the tools or their parameters?
2. Were there any tools you expected to exist but didn't find?
3. Were there any error messages that were unclear or unhelpful?
4. Could the tool descriptions or documentation be improved? How?
5. Did you encounter any surprising behavior from any tool?
6. What was the most awkward or friction-filled part of the workflow?

Please be specific — reference actual tool names and parameters where relevant."""
