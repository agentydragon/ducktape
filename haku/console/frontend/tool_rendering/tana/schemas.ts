// Argument schemas for the `tana-rw` tools the console renders.
//
// tana-rw is a remote server, so its schemas are hand-authored rather than generated. They live
// in their own React-free module because two consumers need them: the widgets in `requests.tsx`,
// and the notification action registry in `../actions.ts`, which the service worker bundles.

import { z } from "zod";

export const zEditOperation: z.ZodType<{ old_string: string; new_string: string; replace_all?: boolean }> = z.object({
  old_string: z.string(),
  new_string: z.string(),
  replace_all: z.boolean().optional(),
});

export const zImportTanaPasteArgs: z.ZodType<{ parentNodeId: string; content: string }> = z.object({
  parentNodeId: z.string(),
  content: z.string(),
});
export const zGetOrCreateCalendarNodeArgs: z.ZodType<{
  workspaceId: string;
  granularity: "day" | "week" | "month" | "year";
  date?: string;
}> = z.object({
  workspaceId: z.string(),
  granularity: z.enum(["day", "week", "month", "year"]),
  date: z.string().optional(),
});
export const zTrashNodeArgs: z.ZodType<{ nodeId: string }> = z.object({ nodeId: z.string() });
export const zEditNodeArgs: z.ZodType<{
  nodeId: string;
  name?: { old_string: string; new_string: string; replace_all?: boolean };
  description?: { old_string: string; new_string: string; replace_all?: boolean };
}> = z
  .object({ nodeId: z.string(), name: zEditOperation.optional(), description: zEditOperation.optional() })
  .refine((args) => args.name !== undefined || args.description !== undefined);
export const zMoveNodeArgs: z.ZodType<{
  nodeId: string;
  targetNodeId: string;
  sourceParentId?: string;
  position: "start" | "end" | "after" | "before";
  referenceNodeId?: string;
  keepSourceReference: boolean;
}> = z.object({
  nodeId: z.string(),
  targetNodeId: z.string(),
  sourceParentId: z.string().optional(),
  position: z.enum(["start", "end", "after", "before"]).default("end"),
  referenceNodeId: z.string().optional(),
  keepSourceReference: z.boolean().default(false),
});
export const zSetFieldOptionArgs: z.ZodType<{
  nodeId: string;
  attributeId: string;
  optionId: string;
  mode: "replace" | "append";
}> = z.object({
  nodeId: z.string(),
  attributeId: z.string(),
  optionId: z.string(),
  mode: z.enum(["replace", "append"]).default("replace"),
});
