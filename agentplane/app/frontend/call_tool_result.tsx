import { Anchor, Badge, Code, Stack, Text } from "@mantine/core";
import { type JSX, useMemo } from "react";
import { z } from "zod";

import { HighlightedText, JsonView, looksLikeJson } from "./json_view";

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
  /** Absent unless the tool returned structured content. */
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
});

/** `value` as a `CallToolResult`, or `null` when it is not one. */
export function parseCallToolResult(value: unknown): CallToolResult | null {
  const parsed = callToolResultSchema.safeParse(value);
  if (!parsed.success) return null;
  const { content, structuredContent, isError } = parsed.data;
  return {
    content: content.map((block): ContentBlock => {
      const drawn = drawnBlockSchema.safeParse(block);
      return drawn.success ? drawn.data : { type: "unrecognized", block };
    }),
    structuredContent,
    isError,
  };
}

/** A `CallToolResult` the way the tool answered it: its content blocks in order, then its
 * structured content. All of it is the tool's untrusted output: text renders as text, never as
 * markup, an `<img>` loads only an `image/*` data URI, and a link opens only a web URL. */
export function CallToolResultView({ result }: { result: CallToolResult }): JSX.Element {
  return (
    <Stack gap="xs">
      {result.isError && (
        <div>
          <Badge color="red">Tool error</Badge>
        </div>
      )}
      {result.content.length === 0 && result.structuredContent === undefined && (
        <Text size="sm" c="dimmed">
          The tool returned no content.
        </Text>
      )}
      {result.content.map((block, index) => (
        <div key={index}>
          <ContentBlockView block={block} />
        </div>
      ))}
      {result.structuredContent !== undefined && (
        <div>
          <Text size="xs" c="dimmed" mb={4}>
            Structured content
          </Text>
          <JsonView value={result.structuredContent} />
        </div>
      )}
    </Stack>
  );
}

function ContentBlockView({ block }: { block: ContentBlock }): JSX.Element {
  switch (block.type) {
    case "text":
      return <TextBlock text={block.text} />;
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
          <TextBlock text={block.resource.text} />
        </div>
      );
    case "unrecognized":
      return <JsonView value={block.block} />;
  }
}

/** Text as the structure it serializes when it is a JSON object or array, which is how tools often
 * answer, and otherwise as the text itself. */
function TextBlock({ text }: { text: string }): JSX.Element {
  const structure = useMemo(() => jsonStructure(text), [text]);
  return structure === undefined ? <HighlightedText text={text} /> : <JsonView value={structure} />;
}

function jsonStructure(text: string): unknown {
  if (!looksLikeJson(text)) return undefined;
  try {
    return JSON.parse(text);
  } catch (error) {
    if (error instanceof SyntaxError) return undefined;
    throw error;
  }
}
