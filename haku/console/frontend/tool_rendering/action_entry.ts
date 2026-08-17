// One registry entry: a tool's one-line action description ("Gmail: Draft email", "kubectl: Delete
// Pod"), optionally computed from its parsed arguments. The per-server maps live beside their
// widgets in `<server>/actions.ts`; `actions.ts` composes them.
//
// Leaf module, and **React-free** like everything on this side of the split: the service worker
// (`../sw.ts`) bundles the action registry to title push notifications, and must not drag React,
// Mantine, and CodeMirror into a bundle the browser loads to show a notification.

import type { z } from "zod";

/** A registered tool's action description. `destructive` is a danger cue (irreversible deletes):
 * the card colors it red, and the notification — which has no red text — says so in words. */
export type ToolAction = { text: string; destructive?: boolean };

export type ActionEntry = { schema?: z.ZodTypeAny; describe: (args: never) => ToolAction };

/** Bind a tool's argument schema to a description computed from its parsed arguments. Pass the
 * schema the widget already uses — generated (`mcpToolSchema`) or, for a proxied server, the
 * hand-authored one in `<server>/schemas.ts` — rather than restating a subset of it here. */
export function fromArgs<S extends z.ZodTypeAny>(schema: S, describe: (args: z.infer<S>) => ToolAction): ActionEntry {
  return { schema, describe: describe as (args: never) => ToolAction };
}

/** A description that does not depend on the arguments — most tools. No schema, so nothing to
 * parse and nothing to keep in step with the widget's. */
export function fixed(text: string, destructive?: boolean): ActionEntry {
  return { describe: () => (destructive ? { text, destructive } : { text }) };
}

/** "4 threads" / "1 item" — a count plus its naively pluralized noun. Deliberately a copy of
 * `vocabulary.tsx`'s, which is React and so cannot be imported from this side of the split. */
export function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}
