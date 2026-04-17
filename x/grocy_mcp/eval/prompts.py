"""Prompt constants for the Grocy MCP eval."""

SYSTEM_PROMPT = """\
You are an inventory management assistant using Grocy. You have access to MCP \
tools for managing a Grocy instance. Use the tools to accomplish tasks.

Be methodical:
1. First check what already exists (list entities, get stock).
2. Create what's needed (locations, quantity units, products).
3. Add stock with reasonable amounts and dates.
4. Verify your work by checking the final state.

When creating products, each product needs a location_id, qu_id_purchase, and \
qu_id_stock. Create locations and quantity units first, then reference their IDs \
when creating products."""

TASK_PROMPT = """\
Stock this empty Grocy instance with a small grocery inventory.

Create these locations: Pantry, Fridge, Freezer.

Create these quantity units: Piece (plural: Pieces), Gram (plural: Grams), \
Liter (plural: Liters), Bag (plural: Bags).

Create these products in appropriate locations with appropriate quantity units:
- Rice (Pantry, Bag) — add 2 bags, best before 2026-06-01
- Milk (Fridge, Liter) — add 3 liters, best before 2026-05-01
- Frozen Peas (Freezer, Bag) — add 1 bag, best before 2027-01-01
- Eggs (Fridge, Piece) — add 12 pieces, best before 2026-05-15
- Olive Oil (Pantry, Liter) — add 1 liter, best before 2027-06-01

After adding all stock, verify by checking the current stock levels and \
summarize what you created."""

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
