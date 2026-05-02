// Smoke-bundle marker entry. Pins highlight.js + svelte-data-table as
// imports a third entry shares with the main app, so esbuild keeps both
// vendor packages alive in the chunk graph (alongside the per-package
// markers in `vendor_highlight_marker.ts` / `vendor_datatable_marker.ts`).

import hljs from "highlight.js";
import { DataTable } from "@careswitch/svelte-data-table";

export const __keepalive = { hljs, DataTable };
