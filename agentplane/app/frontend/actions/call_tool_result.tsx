import { Anchor, Badge, Code, Stack, Text } from "@mantine/core";
import { type JSX, useMemo } from "react";
import { z } from "zod";
import { parseAllDocuments } from "yaml";

import { JsonBlock, TextBlock } from "../text_block";
import { parsedJson } from "../parsed_json";
import { looksLikeJson } from "../../../../util/frontend/transcript/text_block";

/** One block of a `CallToolResult`'s `content`, in the MCP wire shape of the kinds this view
 * draws. `unrecognized` holds any other block as stored: audio, a binary resource, an image whose
 * mime type is not `image/*`, or a kind newer than this code. */
export type ContentBlock =
  | { type: "text"; text: string }
  | { type: "image"; data: string; mimeType: string }
  | { type: "resource_link"; uri: string; name: string; title?: string; description?: string }
  | { type: "resource"; resource: { uri: string; text: string } }
  | { type: "unrecognized"; block: unknown };

/** An MCP `tools/call` result, which is what the Action Service stores for an MCP-backed Action. */
export interface CallToolResult {
  content: ContentBlock[];
  /** The tool's structured return value, absent unless it returned one. FastMCP's `{result: …}`
   * envelope around a return that is not an object is unwrapped. */
  structuredContent?: unknown;
  isError: boolean;
}

// The only type of data URI an `<img>` here is given.
const IMAGE_MIME_TYPE = /^image\/[\w.+-]+$/i;
// The only URIs a resource link opens; any other scheme is shown, not linked.
const WEB_URL = /^https?:\/\//i;

const drawnBlockSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("text"), text: z.string() }),
  z.object({ type: z.literal("image"), data: z.string(), mimeType: z.string().regex(IMAGE_MIME_TYPE) }),
  z.object({
    type: z.literal("resource_link"),
    uri: z.string(),
    name: z.string(),
    title: z.string().optional(),
    description: z.string().optional(),
  }),
  z.object({ type: z.literal("resource"), resource: z.object({ uri: z.string(), text: z.string() }) }),
]);

const callToolResultSchema = z.object({
  content: z.array(z.unknown()),
  structuredContent: z.unknown().optional(),
  isError: z.boolean().default(false),
  _meta: z.object({ fastmcp: z.object({ wrap_result: z.boolean().optional() }).optional() }).optional(),
});

/** FastMCP wraps a return that is not an object as `{result: …}`, and says so in `_meta`. */
function unwrapped(structured: unknown, wrapped: boolean): unknown {
  if (!wrapped || structured === null || typeof structured !== "object" || Array.isArray(structured)) return structured;
  const keys = Object.keys(structured);
  return keys.length === 1 && keys[0] === "result" ? (structured as { result: unknown }).result : structured;
}

/** `value` as a `CallToolResult`, or `null` when it is not one. */
export function parseCallToolResult(value: unknown): CallToolResult | null {
  const parsed = callToolResultSchema.safeParse(value);
  if (!parsed.success) return null;
  const { content, structuredContent, isError, _meta } = parsed.data;
  return {
    content: content.map((block): ContentBlock => {
      const drawn = drawnBlockSchema.safeParse(block);
      return drawn.success ? drawn.data : { type: "unrecognized", block };
    }),
    structuredContent: unwrapped(structuredContent, _meta?.fastmcp?.wrap_result === true),
    isError,
  };
}

/** The tool's own return value: its structured content, else the parsed JSON or plain text in its one text block.
 * `undefined` when it has none, and for an error result, whose content says what went wrong rather
 * than returning a value. */
export function toolValue(result: CallToolResult): unknown {
  if (result.isError) return undefined;
  if (result.structuredContent !== undefined) return result.structuredContent;
  const [only] = result.content;
  if (result.content.length !== 1 || only.type !== "text") return undefined;
  const json = parsedJson(only.text);
  return json === undefined ? only.text : json;
}

/** Whether a custom result view can replace this envelope without hiding distinct tool output. */
export function toolContentRestatesValue(result: CallToolResult, value: unknown): boolean {
  if (result.isError) return false;
  return result.content.every((block) => block.type === "text" && restates(block.text, value));
}

/** A `CallToolResult` the way the tool answered it: its content blocks in order, then its
 * structured content, shown once. A text block that only restates the structured value, as FastMCP
 * writes one for clients that read content alone, is left out. All of it is the tool's untrusted
 * output: text renders as text, never as markup, an `<img>` loads only an `image/*` data URI, and a
 * link opens only a web URL. */
export function CallToolResultView({ result }: { result: CallToolResult }): JSX.Element {
  const structured = result.structuredContent;
  const blocks =
    structured === undefined || result.isError
      ? result.content
      : result.content.filter((block) => block.type !== "text" || !restates(block.text, structured));
  return (
    <Stack gap="xs">
      {result.isError && (
        <div>
          <Badge color="red">Tool error</Badge>
        </div>
      )}
      {blocks.length === 0 && structured === undefined && (
        <Text size="sm" c="dimmed">
          The tool returned no content.
        </Text>
      )}
      {blocks.map((block, index) => (
        <div key={index}>
          <ContentBlockView block={block} />
        </div>
      ))}
      {structured !== undefined && (
        <div>
          <Text size="xs" c="dimmed" mb={4}>
            Structured content
          </Text>
          <JsonBlock value={structured} />
        </div>
      )}
    </Stack>
  );
}

function ContentBlockView({ block }: { block: ContentBlock }): JSX.Element {
  switch (block.type) {
    case "text":
      return <ToolTextBlock text={block.text} />;
    case "image":
      return (
        <img
          src={`data:${block.mimeType};base64,${block.data}`}
          alt={`Image from the tool (${block.mimeType})`}
          style={{ display: "block", maxWidth: "100%" }}
        />
      );
    case "resource_link":
      return (
        <div>
          <Text size="sm">{block.title ?? block.name}</Text>
          <Text size="sm" style={{ overflowWrap: "anywhere" }}>
            {WEB_URL.test(block.uri) ? (
              <Anchor href={block.uri} target="_blank" rel="noreferrer" inherit>
                {block.uri}
              </Anchor>
            ) : (
              <Code>{block.uri}</Code>
            )}
          </Text>
          {block.description && (
            <Text size="xs" c="dimmed">
              {block.description}
            </Text>
          )}
        </div>
      );
    case "resource":
      return (
        <div>
          <Text size="xs" c="dimmed" mb={4} style={{ overflowWrap: "anywhere" }}>
            {block.resource.uri}
          </Text>
          <ToolTextBlock text={block.resource.text} />
        </div>
      );
    case "unrecognized":
      return <JsonBlock value={block.block} />;
  }
}

/** Text as the structure it serializes when it is a JSON object or array, which is how tools often
 * answer, and otherwise as the text itself. */
function ToolTextBlock({ text }: { text: string }): JSX.Element {
  const structure = useMemo(() => jsonStructure(text), [text]);
  return structure === undefined ? (
    <TextBlock text={text} language={looksLikeJson(text) ? "json" : undefined} />
  ) : (
    <JsonBlock value={structure} />
  );
}

/** Whether `text` says nothing but `value`: its JSON, or the string itself. */
function restates(text: string, value: unknown): boolean {
  if (typeof value === "string" && text === value) return true;
  const parsed = parsedJson(text);
  if (parsed !== undefined) return canonicalJson(parsed) === canonicalJson(value);
  try {
    const documents = parseAllDocuments(text, { prettyErrors: false, uniqueKeys: true });
    if (documents.some((document) => document.errors.length > 0)) return false;
    const values = documents.flatMap((document) => (document.contents === null ? [] : [document.toJS()]));
    if (values.length === 0) return false;
    const resources = values.flatMap((document) => (Array.isArray(document) ? document : [document]));
    if (resources.some((resource) => !isKubernetesResourceValue(resource))) return false;
    return canonicalJson(values.length === 1 ? values[0] : values) === canonicalJson(value);
  } catch {
    return false;
  }
}

function isKubernetesResourceValue(value: unknown): boolean {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const resource = value as Record<string, unknown>;
  if (typeof resource.apiVersion !== "string" || typeof resource.kind !== "string") return false;
  const metadata = resource.metadata;
  return metadata === undefined || (metadata !== null && typeof metadata === "object" && !Array.isArray(metadata));
}

/** JSON with every object's keys sorted, so two serializations of one value compare equal. */
function canonicalJson(value: unknown): string {
  return JSON.stringify(value, (_key, nested: unknown) =>
    nested !== null && typeof nested === "object" && !Array.isArray(nested)
      ? Object.fromEntries(Object.entries(nested).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
      : nested
  );
}

function jsonStructure(text: string): unknown {
  return looksLikeJson(text) ? parsedJson(text) : undefined;
}
