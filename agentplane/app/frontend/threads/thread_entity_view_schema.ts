import { zThreadEntityView } from "./thread_entity_view.zod";
import type { z } from "zod";

export const threadEntityViewSchema = zThreadEntityView;
export type ThreadEntityView = z.infer<typeof threadEntityViewSchema>;
