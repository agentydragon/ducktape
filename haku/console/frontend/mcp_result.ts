import { z } from "zod";

// The MCP CallToolResult fields used by both live browser calls and stored audit records. Unknown
// content-block fields remain intact; this adapter only selects the tool's structured payload.
const zCallToolResult = z.looseObject({
  content: z.array(z.looseObject({ type: z.string(), text: z.string().optional() })).optional(),
  isError: z.boolean().optional(),
  structuredContent: z.record(z.string(), z.unknown()).nullish(),
  _meta: z.looseObject({ fastmcp: z.looseObject({ wrap_result: z.boolean().optional() }).optional() }).nullish(),
});

export function mcpToolError(resultJson: unknown): string | null {
  const parsed = zCallToolResult.safeParse(resultJson);
  if (!parsed.success || !parsed.data.isError) return null;
  const messages = (parsed.data.content ?? [])
    .filter((block) => block.type === "text" && block.text !== undefined)
    .map((block) => block.text);
  return messages.join("\n") || "MCP tool returned an error";
}

/** Select the tool's own return value from a CallToolResult. FastMCP wraps non-object returns in
 * `{result: ...}` and marks that exact envelope in `_meta`; object returns stay untouched. */
export function unwrapMcpToolResult(resultJson: unknown): unknown {
  const parsed = zCallToolResult.safeParse(resultJson);
  if (!parsed.success || parsed.data.isError) return null;
  const structured = parsed.data.structuredContent;
  if (structured != null) {
    const wrapped =
      parsed.data._meta?.fastmcp?.wrap_result === true &&
      "result" in structured &&
      Object.keys(structured).length === 1;
    return wrapped ? structured.result : structured;
  }
  const blocks = parsed.data.content ?? [];
  if (blocks.length === 1 && blocks[0].type === "text" && blocks[0].text !== undefined) {
    try {
      return JSON.parse(blocks[0].text);
    } catch {
      return null;
    }
  }
  return null;
}
