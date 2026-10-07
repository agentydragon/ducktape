# Grocy MCP

The tool descriptions and JSON schemas are the authoritative operating guide. Prefer the typed product, stock, location,
quantity-unit, product-group, and shopping-list tools: they resolve references and expose operation-specific safety
constraints. Use the generic `entities_*` tools for recipes, chores, tasks, equipment, meal plans, and other entity
types without a typed wrapper.

Parameters documented as “Name or ID” accept either an integer Grocy ID or the entity's unique name, matched
case-insensitively. Typed shopping-list tools hide Grocy's internal split between `shopping_lists` metadata and
`shopping_list` items.
