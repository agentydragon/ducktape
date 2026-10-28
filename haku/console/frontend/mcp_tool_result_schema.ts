// Runtime validators and static result types derived from the exact *output* schemas advertised by
// the in-process FastMCP servers — the result-side mirror of mcp_tool_schema.ts, plus grocy-sf's
// typed batch-tool results. Only tools with a structured output schema have an entry, so this is a
// subset of the argument catalog; renderer-specific projections may narrow permissive
// `extra="allow"` Grocy rows further. The producer-owned JSON catalog and its generated declarations
// describe the same contract; like the argument side, this is the only boundary where the experimental
// z.fromJSONSchema API is used.

import type { z } from "zod";

import type { McpToolResults } from "../mcp/tool_results";
import mcpToolResultsSchema from "../mcp/tool_results.schema.json";
import { compileToolSchemaCatalog, type CompiledToolSchema, type JsonSchema } from "./mcp_tool_schema";

export type McpToolResultServerId = Extract<keyof McpToolResults, string>;
export type McpToolResultName<S extends McpToolResultServerId> = Extract<keyof McpToolResults[S], string>;
export type McpToolResultFor<S extends McpToolResultServerId, T extends McpToolResultName<S>> = McpToolResults[S][T];

// Eagerly compile the whole catalog, not just tools that currently have result widgets, so a newly
// advertised output schema that Zod cannot represent fails tests and startup rather than quietly
// weakening a result preview.
export const mcpToolResultSchemas: readonly CompiledToolSchema[] = Object.freeze(
  compileToolSchemaCatalog(mcpToolResultsSchema as unknown as JsonSchema, {
    catalogContext: "MCP tool result schema catalog",
    serverContext: (serverId) => `MCP server result schema ${serverId}`,
    compilationError: (serverId, toolName, detail) =>
      `Cannot build result validator for MCP tool ${serverId}.${toolName}: ${detail}`,
    emptyCatalogError: "MCP tool result schema catalog contains no tools",
  })
);

export function mcpToolResultSchema<S extends McpToolResultServerId, T extends McpToolResultName<S>>(
  serverId: S,
  toolName: T
): z.ZodType<McpToolResultFor<S, T>> {
  const entry = mcpToolResultSchemas.find(
    (candidate) => candidate.serverId === serverId && candidate.toolName === toolName
  );
  if (!entry) throw new Error(`No generated result schema for MCP tool ${serverId}.${toolName}`);
  // JSON Schema owns both artifacts, so the generated static type and runtime validator describe
  // the same value. Keep the assertion at this codegen boundary rather than in each result widget.
  return entry.schema as z.ZodType<McpToolResultFor<S, T>>;
}
