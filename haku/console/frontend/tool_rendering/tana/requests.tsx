// Focused previews for the remote, operator-authenticated `tana-rw` MCP server. The
// desktop-backed server only receives opaque node ids, so the browser resolves names by calling
// read_node through the console's same-origin Operator MCP session.

import { Anchor, Group, Stack } from "@mantine/core";
import { useEffect, useState } from "react";
import { z } from "zod";

import { CodeBlock } from "../../code_block.tsx";
import { Field } from "../../field.tsx";
import { fetchTanaNodePreviews, type TanaNodePreview } from "../../tana_client.ts";
import { definePreview, type ToolPreview } from "../entry.tsx";
import { clampBlock, PreviewBadge, PreviewText, type PreviewProps } from "../vocabulary.tsx";

export const TANA_RW_SERVER_ID = "tana-rw";

const zEditOperation = z.object({
  old_string: z.string(),
  new_string: z.string(),
  replace_all: z.boolean().optional(),
});

const zImportTanaPasteArgs = z.object({ parentNodeId: z.string(), content: z.string() });
const zGetOrCreateCalendarNodeArgs = z.object({
  workspaceId: z.string(),
  granularity: z.enum(["day", "week", "month", "year"]),
  date: z.string().optional(),
});
const zTrashNodeArgs = z.object({ nodeId: z.string() });
const zEditNodeArgs = z
  .object({ nodeId: z.string(), name: zEditOperation.optional(), description: zEditOperation.optional() })
  .refine((args) => args.name !== undefined || args.description !== undefined);
const zMoveNodeArgs = z.object({
  nodeId: z.string(),
  targetNodeId: z.string(),
  sourceParentId: z.string().optional(),
  position: z.enum(["start", "end", "after", "before"]).default("end"),
  referenceNodeId: z.string().optional(),
  keepSourceReference: z.boolean().default(false),
});
const zSetFieldOptionArgs = z.object({
  nodeId: z.string(),
  attributeId: z.string(),
  optionId: z.string(),
  mode: z.enum(["replace", "append"]).default("replace"),
});

type ImportTanaPasteArgs = z.infer<typeof zImportTanaPasteArgs>;
type GetOrCreateCalendarNodeArgs = z.infer<typeof zGetOrCreateCalendarNodeArgs>;
type TrashNodeArgs = z.infer<typeof zTrashNodeArgs>;
type EditNodeArgs = z.infer<typeof zEditNodeArgs>;
type MoveNodeArgs = z.infer<typeof zMoveNodeArgs>;
type SetFieldOptionArgs = z.infer<typeof zSetFieldOptionArgs>;

function tanaNodeUrl(nodeId: string): string {
  return `https://app.tana.inc?nodeid=${encodeURIComponent(nodeId)}`;
}

function useTanaNodePreviews(nodeIds: string[]): Record<string, TanaNodePreview> | null {
  const [previews, setPreviews] = useState<Record<string, TanaNodePreview> | null>(null);
  const key = [...new Set(nodeIds)].sort().join(",");

  useEffect(() => {
    let alive = true;
    setPreviews(null);
    fetchTanaNodePreviews(key === "" ? [] : key.split(",")).then(
      (result) => {
        if (alive) setPreviews(result);
      },
      () => {
        if (alive) setPreviews({});
      }
    );
    return () => {
      alive = false;
    };
  }, [key]);

  return previews;
}

function TanaNodeLink({ nodeId, previews }: { nodeId: string; previews: Record<string, TanaNodePreview> | null }) {
  const preview = previews?.[nodeId];
  return preview ? (
    <Anchor href={tanaNodeUrl(nodeId)} target="_blank" rel="noreferrer" size="sm">
      {preview.name}
    </Anchor>
  ) : (
    <PreviewText span className="haku-shell-mono" c="dimmed">
      {nodeId}
    </PreviewText>
  );
}

function ImportTanaPastePreview({ args, variant }: PreviewProps<ImportTanaPasteArgs>) {
  const previews = useTanaNodePreviews([args.parentNodeId]);
  const content = variant === "compact" ? clampBlock(args.content, 3) : args.content;
  return (
    <Stack gap="xs">
      <Field label="Under">
        <TanaNodeLink nodeId={args.parentNodeId} previews={previews} />
      </Field>
      <CodeBlock value={content} />
    </Stack>
  );
}

function GetOrCreateCalendarNodePreview({ args }: PreviewProps<GetOrCreateCalendarNodeArgs>) {
  return (
    <Group gap={6}>
      <PreviewBadge variant="outline">{args.granularity}</PreviewBadge>
      {args.date && <PreviewText>{args.date}</PreviewText>}
      <PreviewText c="dimmed" className="haku-shell-mono">
        {args.workspaceId}
      </PreviewText>
    </Group>
  );
}

function TrashNodePreview({ args }: PreviewProps<TrashNodeArgs>) {
  const previews = useTanaNodePreviews([args.nodeId]);
  return <TanaNodeLink nodeId={args.nodeId} previews={previews} />;
}

function EditOperation({ label, edit }: { label: string; edit: z.infer<typeof zEditOperation> }) {
  return (
    <Stack gap={2}>
      <PreviewText fw={600}>{label}</PreviewText>
      <PreviewText style={{ whiteSpace: "pre-wrap" }}>
        <PreviewText span c="dimmed">
          {edit.old_string || "(empty)"}
        </PreviewText>
        {" → "}
        {edit.new_string || "(clear)"}
        {edit.replace_all && (
          <PreviewText span c="dimmed">
            {" · all matches"}
          </PreviewText>
        )}
      </PreviewText>
    </Stack>
  );
}

function EditNodePreview({ args }: PreviewProps<EditNodeArgs>) {
  const previews = useTanaNodePreviews([args.nodeId]);
  return (
    <Stack gap="xs">
      <TanaNodeLink nodeId={args.nodeId} previews={previews} />
      {args.name && <EditOperation label="Name" edit={args.name} />}
      {args.description && <EditOperation label="Description" edit={args.description} />}
    </Stack>
  );
}

function MoveNodePreview({ args }: PreviewProps<MoveNodeArgs>) {
  const previews = useTanaNodePreviews(
    [args.nodeId, args.targetNodeId, args.sourceParentId, args.referenceNodeId].filter(
      (id): id is string => id !== undefined
    )
  );
  return (
    <Stack gap={4}>
      <Group gap={6}>
        <TanaNodeLink nodeId={args.nodeId} previews={previews} />
        <PreviewText c="dimmed">→</PreviewText>
        <TanaNodeLink nodeId={args.targetNodeId} previews={previews} />
      </Group>
      <Group gap={6}>
        <PreviewBadge variant="outline">{args.position}</PreviewBadge>
        {args.referenceNodeId && <TanaNodeLink nodeId={args.referenceNodeId} previews={previews} />}
        {args.sourceParentId && (
          <PreviewText c="dimmed">
            from <TanaNodeLink nodeId={args.sourceParentId} previews={previews} />
          </PreviewText>
        )}
        {args.keepSourceReference && <PreviewBadge variant="outline">keep reference</PreviewBadge>}
      </Group>
    </Stack>
  );
}

function SetFieldOptionPreview({ args }: PreviewProps<SetFieldOptionArgs>) {
  const previews = useTanaNodePreviews([args.nodeId, args.attributeId, args.optionId]);
  return (
    <Stack gap="xs">
      <Field label="Node">
        <TanaNodeLink nodeId={args.nodeId} previews={previews} />
      </Field>
      <Field label="Field">
        <TanaNodeLink nodeId={args.attributeId} previews={previews} />
      </Field>
      <Field label="Option">
        <Group gap={6}>
          <PreviewBadge variant="outline">{args.mode}</PreviewBadge>
          <TanaNodeLink nodeId={args.optionId} previews={previews} />
        </Group>
      </Field>
    </Stack>
  );
}

export const tanaPreviews = {
  import_tana_paste: definePreview(zImportTanaPasteArgs, ImportTanaPastePreview, () => ({
    text: "Tana: Import content",
  })),
  get_or_create_calendar_node: definePreview(zGetOrCreateCalendarNodeArgs, GetOrCreateCalendarNodePreview, () => ({
    text: "Tana: Get or create calendar node",
  })),
  trash_node: definePreview(zTrashNodeArgs, TrashNodePreview, () => ({ text: "Tana: Trash node", destructive: true })),
  edit_node: definePreview(zEditNodeArgs, EditNodePreview, () => ({ text: "Tana: Edit node" })),
  move_node: definePreview(zMoveNodeArgs, MoveNodePreview, () => ({ text: "Tana: Move node" })),
  set_field_option: definePreview(zSetFieldOptionArgs, SetFieldOptionPreview, (a) => ({
    text: `Tana: ${a.mode === "append" ? "Append" : "Set"} field option`,
  })),
} satisfies Record<string, ToolPreview>;
