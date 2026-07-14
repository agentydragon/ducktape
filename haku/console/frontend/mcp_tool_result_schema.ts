// Runtime validators and static result types derived from the exact *output* schemas advertised
// by the in-process FastMCP servers — the result-side mirror of mcp_tool_schema.ts. A tool that
// returns None, or whose result the frontend renders as raw JSON, has no entry; the catalog only
// carries tools with a structured output schema (so it is a subset of the argument catalog).
// grocy-sf is absent: its result schemas stay hand-authored in tool_rendering/grocy/responses.tsx.
// The JSON catalog and .d.ts module are two outputs of the same generator; like the argument
// side, this is the only boundary where the experimental z.fromJSONSchema API is used.

import { z } from "zod";

import type { McpToolResults } from "./api/mcp_tool_results";
import mcpToolResultsSchema from "./api/mcp_tool_results.schema.json";
import { objectProperties } from "./mcp_tool_schema.ts";

type JsonSchema = Parameters<typeof z.fromJSONSchema>[0];

export type McpToolResultServerId = Extract<keyof McpToolResults, string>;
export type McpToolResultName<S extends McpToolResultServerId> = Extract<keyof McpToolResults[S], string>;
export type McpToolResultFor<S extends McpToolResultServerId, T extends McpToolResultName<S>> = McpToolResults[S][T];

type CompiledResultSchema = {
  serverId: string;
  toolName: string;
  schema: z.ZodType;
};

function compileResultCatalog(): CompiledResultSchema[] {
  // `as unknown as JsonSchema` (rather than the plain `as JsonSchema` the argument catalog uses):
  // grocy-sf's nested array-of-union result schemas widen the JSON module's inferred type past what
  // TS considers structurally overlapping with JsonSchema, though the value is valid JSON Schema.
  const servers = objectProperties(mcpToolResultsSchema as unknown as JsonSchema, "MCP tool result schema catalog");
  const compiled: CompiledResultSchema[] = [];

  for (const [serverId, serverSchema] of Object.entries(servers)) {
    const tools = objectProperties(serverSchema, `MCP server result schema ${serverId}`);
    for (const [toolName, toolSchema] of Object.entries(tools)) {
      try {
        compiled.push({ serverId, toolName, schema: z.fromJSONSchema(toolSchema) });
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        throw new Error(`Cannot build result validator for MCP tool ${serverId}.${toolName}: ${detail}`, {
          cause: error,
        });
      }
    }
  }

  if (compiled.length === 0) throw new Error("MCP tool result schema catalog contains no tools");
  return compiled;
}

// Eagerly compile the whole catalog, not just tools that currently have result widgets — the
// argument-side counterpart's rationale applies: a newly advertised output schema that Zod cannot
// represent fails tests and startup rather than quietly weakening a result preview.
export const mcpToolResultSchemas: readonly CompiledResultSchema[] = Object.freeze(compileResultCatalog());

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
