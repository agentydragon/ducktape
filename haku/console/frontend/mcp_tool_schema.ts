// Runtime validators and static argument types derived from the exact schemas advertised by
// the in-process FastMCP servers. They cover JSON-Schema-expressible structure; execution-only
// Python cross-field validators may be stricter. The JSON catalog and .d.ts module are two
// outputs of the same generator; this is the only boundary where the experimental
// z.fromJSONSchema API is used.

import { z } from "zod";

import type { McpToolArguments } from "./api/mcp_tool_arguments";
import mcpToolArgumentsSchema from "./api/mcp_tool_arguments.schema.json";

type JsonSchema = Parameters<typeof z.fromJSONSchema>[0];
type ObjectSchema = Exclude<JsonSchema, boolean> & {
  properties?: Record<string, JsonSchema>;
};

export type McpServerId = Extract<keyof McpToolArguments, string>;
export type McpToolName<S extends McpServerId> = Extract<keyof McpToolArguments[S], string>;
export type McpToolArgumentsFor<S extends McpServerId, T extends McpToolName<S>> = McpToolArguments[S][T];

type CompiledSchema = {
  serverId: string;
  toolName: string;
  schema: z.ZodType;
};

/** Read the `properties` map off an object JSON Schema, throwing a labeled error otherwise.
 * Shared with mcp_tool_result_schema.ts so both catalogs walk their server/tool tree identically. */
export function objectProperties(schema: JsonSchema, context: string): Record<string, JsonSchema> {
  if (typeof schema === "boolean" || schema.type !== "object" || !schema.properties) {
    throw new Error(`${context} must be an object JSON Schema with properties`);
  }
  return (schema as ObjectSchema).properties ?? {};
}

function compileCatalog(): CompiledSchema[] {
  const servers = objectProperties(mcpToolArgumentsSchema as JsonSchema, "MCP tool schema catalog");
  const compiled: CompiledSchema[] = [];

  for (const [serverId, serverSchema] of Object.entries(servers)) {
    const tools = objectProperties(serverSchema, `MCP server schema ${serverId}`);
    for (const [toolName, toolSchema] of Object.entries(tools)) {
      try {
        compiled.push({ serverId, toolName, schema: z.fromJSONSchema(toolSchema) });
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        throw new Error(`Cannot build validator for MCP tool ${serverId}.${toolName}: ${detail}`, { cause: error });
      }
    }
  }

  if (compiled.length === 0) throw new Error("MCP tool schema catalog contains no tools");
  return compiled;
}

// Eagerly compile the complete catalog, not just tools that currently have custom previews, so a
// newly advertised schema that Zod cannot represent fails tests and application startup instead of
// quietly weakening validation for whichever preview eventually consumes it.
export const mcpToolSchemas: readonly CompiledSchema[] = Object.freeze(compileCatalog());

export function mcpToolSchema<S extends McpServerId, T extends McpToolName<S>>(
  serverId: S,
  toolName: T
): z.ZodType<McpToolArgumentsFor<S, T>> {
  const entry = mcpToolSchemas.find((candidate) => candidate.serverId === serverId && candidate.toolName === toolName);
  if (!entry) throw new Error(`No generated input schema for MCP tool ${serverId}.${toolName}`);
  // JSON Schema owns both artifacts, so the generated static type and runtime validator describe
  // the same value. Keep the assertion here at that codegen boundary rather than in each preview.
  return entry.schema as z.ZodType<McpToolArgumentsFor<S, T>>;
}
