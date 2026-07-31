// Focused previews for the remote, operator-authenticated `tana-rw` MCP server. The
// desktop-backed server only receives opaque node ids, so the browser resolves names by calling
// read_node through the console's same-origin Operator MCP session.

import { Group, Stack } from "@mantine/core";
import { useEffect, useState } from "react";
import { z } from "zod";

import { CodeBlock } from "../../code_block";
import { Field } from "../../field";
import { ExternalLink } from "../../link";
import { fetchTanaNodePreviews, type TanaNodePreview } from "../../tana_client";
import { definePreview, type ToolPreview } from "../entry";
import { clampBlock, PreviewBadge, PreviewText, type PreviewProps } from "../vocabulary";
import { TANA_RW_SERVER_ID } from "../server_ids";
import {
  zEditNodeArgs,
  zEditOperation,
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
    <ExternalLink href={tanaNodeUrl(nodeId)} size="sm">
      {preview.name}
    </ExternalLink>
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
  import_tana_paste: definePreview(zImportTanaPasteArgs, ImportTanaPastePreview),
  get_or_create_calendar_node: definePreview(zGetOrCreateCalendarNodeArgs, GetOrCreateCalendarNodePreview),
  trash_node: definePreview(zTrashNodeArgs, TrashNodePreview),
  edit_node: definePreview(zEditNodeArgs, EditNodePreview),
  move_node: definePreview(zMoveNodeArgs, MoveNodePreview),
  set_field_option: definePreview(zSetFieldOptionArgs, SetFieldOptionPreview),
} satisfies Record<string, ToolPreview>;
