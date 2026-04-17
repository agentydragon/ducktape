"""Prompt constants for the Grocy MCP eval."""

SYSTEM_PROMPT = "You are an inventory management assistant using Grocy."

TASK_PROMPT = """\
Please stock this empty Grocy instance with what we have on hand.

In the pantry we've got 2 bags of rice that are good through June 2026, plus a \
liter of olive oil that keeps until mid-2027. The fridge has 3 liters of milk \
expiring 2026-05-01 and a dozen eggs expiring 2026-05-15. And in the freezer \
there's a bag of frozen peas, best before 2027-01-01.

Once you've got everything in, summarize what you entered."""

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
