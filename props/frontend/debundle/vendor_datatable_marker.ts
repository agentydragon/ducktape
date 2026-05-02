// Smoke-bundle marker for @careswitch/svelte-data-table. Forces esbuild's
// chunk-graph algorithm to put svelte-data-table into its own shared chunk.

import { DataTable } from "@careswitch/svelte-data-table";

export const __dataTableAlive = DataTable;
