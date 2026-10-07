// Focused previews for stored calls to the remote, operator-authenticated `tana` MCP server. Call
// records contain opaque node IDs, so previews link those IDs directly without reading Tana.

import { Group, Stack } from "@mantine/core";
import type { z } from "zod";

import { CodeBlock } from "../../code_block";
import { Field } from "../../field";
import { ExternalLink } from "../../link";
import { definePreview, type ToolPreview } from "../entry";
import { clampBlock, PreviewBadge, PreviewText, type PreviewProps } from "../vocabulary";
import {
  type zEditOperation,
  zEditNodeArgs,
  zGetOrCreateCalendarNodeArgs,
  zImportTanaPasteArgs,
  zMoveNodeArgs,
  zSetFieldOptionArgs,
  zTrashNodeArgs,
} from "./schemas";

type ImportTanaPasteArgs = z.infer<typeof zImportTanaPasteArgs>;
type GetOrCreateCalendarNodeArgs = z.infer<typeof zGetOrCreateCalendarNodeArgs>;
type TrashNodeArgs = z.infer<typeof zTrashNodeArgs>;
type EditNodeArgs = z.infer<typeof zEditNodeArgs>;
type MoveNodeArgs = z.infer<typeof zMoveNodeArgs>;
type SetFieldOptionArgs = z.infer<typeof zSetFieldOptionArgs>;

function tanaNodeUrl(nodeId: string): string {
  return `https://app.tana.inc?nodeid=${encodeURIComponent(nodeId)}`;
}

function TanaNodeLink({ nodeId }: { nodeId: string }) {
  return (
    <ExternalLink href={tanaNodeUrl(nodeId)} size="sm" className="haku-shell-mono">
      {nodeId}
    </ExternalLink>
  );
}

function ImportTanaPastePreview({ args, variant }: PreviewProps<ImportTanaPasteArgs>) {
  const content = variant === "compact" ? clampBlock(args.content, 3) : args.content;
  return (
    <Stack gap="xs">
      <Field label="Under">
        <TanaNodeLink nodeId={args.parentNodeId} />
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
  return <TanaNodeLink nodeId={args.nodeId} />;
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
  return (
    <Stack gap="xs">
      <TanaNodeLink nodeId={args.nodeId} />
      {args.name && <EditOperation label="Name" edit={args.name} />}
      {args.description && <EditOperation label="Description" edit={args.description} />}
    </Stack>
  );
}

function MoveNodePreview({ args }: PreviewProps<MoveNodeArgs>) {
  return (
    <Stack gap={4}>
      <Group gap={6}>
        <TanaNodeLink nodeId={args.nodeId} />
        <PreviewText c="dimmed">→</PreviewText>
        <TanaNodeLink nodeId={args.targetNodeId} />
      </Group>
      <Group gap={6}>
        <PreviewBadge variant="outline">{args.position}</PreviewBadge>
        {args.referenceNodeId && <TanaNodeLink nodeId={args.referenceNodeId} />}
        {args.sourceParentId && (
          <PreviewText c="dimmed">
            from <TanaNodeLink nodeId={args.sourceParentId} />
          </PreviewText>
        )}
        {args.keepSourceReference && <PreviewBadge variant="outline">keep reference</PreviewBadge>}
      </Group>
    </Stack>
  );
}

function SetFieldOptionPreview({ args }: PreviewProps<SetFieldOptionArgs>) {
  return (
    <Stack gap="xs">
      <Field label="Node">
        <TanaNodeLink nodeId={args.nodeId} />
      </Field>
      <Field label="Field">
        <TanaNodeLink nodeId={args.attributeId} />
      </Field>
      <Field label="Option">
        <Group gap={6}>
          <PreviewBadge variant="outline">{args.mode}</PreviewBadge>
          <TanaNodeLink nodeId={args.optionId} />
        </Group>
      </Field>
    </Stack>
  );
}

export const tanaPreviews: {
  import_tana_paste: ToolPreview<typeof zImportTanaPasteArgs>;
  get_or_create_calendar_node: ToolPreview<typeof zGetOrCreateCalendarNodeArgs>;
  trash_node: ToolPreview<typeof zTrashNodeArgs>;
  edit_node: ToolPreview<typeof zEditNodeArgs>;
  move_node: ToolPreview<typeof zMoveNodeArgs>;
  set_field_option: ToolPreview<typeof zSetFieldOptionArgs>;
} = {
  import_tana_paste: definePreview(zImportTanaPasteArgs, ImportTanaPastePreview),
  get_or_create_calendar_node: definePreview(zGetOrCreateCalendarNodeArgs, GetOrCreateCalendarNodePreview),
  trash_node: definePreview(zTrashNodeArgs, TrashNodePreview),
  edit_node: definePreview(zEditNodeArgs, EditNodePreview),
  move_node: definePreview(zMoveNodeArgs, MoveNodePreview),
  set_field_option: definePreview(zSetFieldOptionArgs, SetFieldOptionPreview),
} satisfies Record<string, ToolPreview>;
