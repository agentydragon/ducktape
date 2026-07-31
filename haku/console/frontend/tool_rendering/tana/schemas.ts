// Argument schemas for the `tana-rw` tools the console renders.
//
// tana-rw is a remote server, so its schemas are hand-authored rather than generated. They live
// in their own React-free module because two consumers need them: the widgets in `requests.tsx`,
// and the notification action registry in `../actions.ts`, which the service worker bundles.

import { z } from "zod";

export const zEditOperation = z.object({
  old_string: z.string(),
  new_string: z.string(),
  replace_all: z.boolean().optional(),
});

export const zImportTanaPasteArgs = z.object({ parentNodeId: z.string(), content: z.string() });
export const zGetOrCreateCalendarNodeArgs = z.object({
  workspaceId: z.string(),
  granularity: z.enum(["day", "week", "month", "year"]),
  date: z.string().optional(),
});
export const zTrashNodeArgs = z.object({ nodeId: z.string() });
export const zEditNodeArgs = z
  .object({ nodeId: z.string(), name: zEditOperation.optional(), description: zEditOperation.optional() })
  .refine((args) => args.name !== undefined || args.description !== undefined);
export const zMoveNodeArgs = z.object({
  nodeId: z.string(),
  targetNodeId: z.string(),
  sourceParentId: z.string().optional(),
  position: z.enum(["start", "end", "after", "before"]).default("end"),
  referenceNodeId: z.string().optional(),
  keepSourceReference: z.boolean().default(false),
});
export const zSetFieldOptionArgs = z.object({
  nodeId: z.string(),
  attributeId: z.string(),
  optionId: z.string(),
  mode: z.enum(["replace", "append"]).default("replace"),
});
