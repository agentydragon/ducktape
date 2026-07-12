// Result rendering for the remote `grocy-sf` server's batch tools (the argument-side widgets
// live in ./requests.tsx). Each batch tool returns one row per input item — `kind:
// "ok"` with per-op details, or a failing kind with an `error` message — so the widgets show
// an ok/failed count summary (compact adds the first few product names) and, detailed, every
// row with its amounts/units/locations, failed rows in red. Hand-authored against
// grocy_mcp/mcp_types.py's StockOpOk / CreateOk / ShoppingListItemOk (+ their error rows) and
// lenient about extra keys, since the remote server's output schemas are not in the build-time
// catalog and can grow fields under the console.

import { Group, Stack } from "@mantine/core";
import type { ReactNode } from "react";
import { z } from "zod";

import {
  COMPACT_ITEM_LIMIT,
  MoreLine,
  PreviewBadge,
  PreviewText,
  PreviewTitle,
  type PreviewVariant,
} from "../vocabulary.tsx";
import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry.tsx";

// A failed row: any kind other than "ok", with the server's error message. Matched after the
// ok row shape in each union, so a malformed "ok" row fails the whole parse (→ raw JSON
// fallback) instead of rendering as a failure.
const zFailedRow = z.looseObject({
  kind: z.string().refine((kind) => kind !== "ok"),
  error: z.string(),
});

// grocy_mcp's StockOpOk. `new_amount`/`entry_id`/`best_before_date` are best-effort on the
// server (null when a follow-up read fails), so nullish here.
const zStockAddOkRow = z.looseObject({
  kind: z.literal("ok"),
  product_name: z.string(),
  amount_delta: z.number().nullish(),
  new_amount: z.number().nullish(),
  qu_name: z.string(),
  location_name: z.string(),
  best_before_date: z.string().nullish(),
});

// grocy_mcp's CreateOk carries only `created_object_id` today; `product_name` is accepted so a
// server that starts naming its rows renders names without a schema change here.
const zProductsCreateOkRow = z.looseObject({
  kind: z.literal("ok"),
  created_object_id: z.number().nullish(),
  product_name: z.string().nullish(),
});

// grocy_mcp's ShoppingListItemOk. `product_name`/`qu_name` are null for note-only items.
const zShoppingListItemOkRow = z.looseObject({
  kind: z.literal("ok"),
  item_id: z.number(),
  product_name: z.string().nullish(),
  amount: z.number(),
  qu_name: z.string().nullish(),
});

const zStockEntryEditOkRow = z.looseObject({
  kind: z.literal("ok"),
  entry: z.looseObject({
    entry_id: z.number(),
    product_name: z.string(),
    amount: z.number(),
    qu_name: z.string(),
    location_name: z.string(),
    best_before_date: z.string().nullish(),
    open: z.boolean(),
  }),
  changes: z.record(z.string(), z.looseObject({ old: z.unknown(), new: z.unknown() })).nullish(),
});
const zStockEntryRow = z.looseObject({
  product_name: z.string(),
  amount: z.number(),
  amount_opened: z.number(),
  qu_name: z.string(),
  location_name: z.string(),
  best_before_date: z.string().nullish(),
});
const zNamedRow = z.looseObject({ id: z.number(), name: z.string() });
const zQuantityUnitRow = zNamedRow.extend({ name_plural: z.string().nullish() });
const zShoppingListGetResult = z.looseObject({
  name: z.string(),
  description: z.string().nullish(),
  items: z.array(
    z.looseObject({
      item_id: z.number(),
      product_name: z.string().nullish(),
      amount: z.number(),
      qu_name: z.string().nullish(),
      note: z.string().nullish(),
      done: z.boolean(),
    })
  ),
});

const zStockAddResult = z.array(z.union([zStockAddOkRow, zFailedRow]));
const zProductsCreateResult = z.array(z.union([zProductsCreateOkRow, zFailedRow]));
const zShoppingListItemsAddResult = z.array(z.union([zShoppingListItemOkRow, zFailedRow]));
const zStockEntryEditResult = z.array(z.union([zStockEntryEditOkRow, zFailedRow]));
const zStockGetResult = z.array(zStockEntryRow);
const zProductsListResult = z.array(zNamedRow);
const zQuantityUnitsListResult = z.array(zQuantityUnitRow);
const zSystemInfoResult = z.looseObject({});
const zShoppingListItemsRemoveResult = z.array(z.union([zShoppingListItemOkRow, zFailedRow]));

type FailedRow = z.infer<typeof zFailedRow>;
type StockAddOkRow = z.infer<typeof zStockAddOkRow>;
type ProductsCreateOkRow = z.infer<typeof zProductsCreateOkRow>;
type ShoppingListItemOkRow = z.infer<typeof zShoppingListItemOkRow>;
type StockEntryEditOkRow = z.infer<typeof zStockEntryEditOkRow>;

// The zFailedRow refine guarantees a non-"ok" kind at runtime, but zod types it as plain
// `string` (and TS doesn't narrow a generic union by discriminant), so the ok/failed split
// casts by kind here, once for every widget.
function splitRows<Ok extends { kind: "ok" }>(rows: readonly (Ok | FailedRow)[]): { ok: Ok[]; failed: FailedRow[] } {
  const ok: Ok[] = [];
  const failed: FailedRow[] = [];
  for (const row of rows) {
    if (row.kind === "ok") ok.push(row as Ok);
    else failed.push(row as FailedRow);
  }
  return { ok, failed };
}

// "3 added (+1 failed)" — the failed count is the one red cue, per the semantic-color rule.
function ResultSummary({ okCount, failedCount, verb }: { okCount: number; failedCount: number; verb: string }) {
  return (
    <Group gap={6}>
      <PreviewText span fw={600}>
        {okCount} {verb}
      </PreviewText>
      {failedCount > 0 && (
        <PreviewText span c="red">
          +{failedCount} failed
        </PreviewText>
      )}
    </Group>
  );
}

/** Shared shape of all three batch-result widgets: the count summary, then compact's first few
 * row names (+ "… +N more") or detailed's full per-row lines with failed rows in red. */
function BatchResultView<Ok extends { kind: "ok" }>({
  rows,
  variant,
  verb,
  rowName,
  RowView,
}: {
  rows: readonly (Ok | FailedRow)[];
  variant: PreviewVariant;
  verb: string;
  rowName: (row: Ok) => string;
  RowView: (props: { row: Ok }) => ReactNode;
}) {
  const { ok, failed } = splitRows(rows);
  if (variant === "compact") {
    const shown = ok.slice(0, COMPACT_ITEM_LIMIT);
    return (
      <Stack gap={2}>
        <ResultSummary okCount={ok.length} failedCount={failed.length} verb={verb} />
        {shown.length > 0 && <PreviewText c="dimmed">{shown.map(rowName).join(" · ")}</PreviewText>}
        <MoreLine count={ok.length - shown.length} />
      </Stack>
    );
  }
  return (
    <Stack gap={4}>
      <ResultSummary okCount={ok.length} failedCount={failed.length} verb={verb} />
      {ok.map((row, i) => (
        <RowView key={i} row={row} />
      ))}
      {failed.map((row, i) => (
        <PreviewText key={i} c="red">
          ✗ {row.error}
        </PreviewText>
      ))}
    </Stack>
  );
}

function StockAddResultRow({ row }: { row: StockAddOkRow }) {
  return (
    <Group gap={6}>
      <PreviewText span fw={600}>
        {row.product_name}
      </PreviewText>
      {row.amount_delta != null && (
        <PreviewText span>
          +{row.amount_delta} {row.qu_name}
        </PreviewText>
      )}
      <PreviewText span c="dimmed">
        {row.new_amount != null ? `→ ${row.new_amount} ${row.qu_name} in ${row.location_name}` : row.location_name}
      </PreviewText>
      {row.best_before_date && <PreviewBadge variant="outline">best before {row.best_before_date}</PreviewBadge>}
    </Group>
  );
}

function StockAddResultView({ result, variant }: ResultPreviewProps<z.infer<typeof zStockAddResult>>) {
  return (
    <BatchResultView
      rows={result}
      variant={variant}
      verb="added"
      rowName={(row) => row.product_name}
      RowView={StockAddResultRow}
    />
  );
}

function productsCreateRowName(row: ProductsCreateOkRow): string {
  return row.product_name ?? (row.created_object_id != null ? `product #${row.created_object_id}` : "product");
}

function ProductsCreateResultRow({ row }: { row: ProductsCreateOkRow }) {
  return (
    <Group gap={6}>
      <PreviewText span fw={600}>
        {productsCreateRowName(row)}
      </PreviewText>
      {row.product_name != null && row.created_object_id != null && (
        <PreviewText span c="dimmed">
          #{row.created_object_id}
        </PreviewText>
      )}
    </Group>
  );
}

function ProductsCreateResultView({ result, variant }: ResultPreviewProps<z.infer<typeof zProductsCreateResult>>) {
  return (
    <BatchResultView
      rows={result}
      variant={variant}
      verb="created"
      rowName={productsCreateRowName}
      RowView={ProductsCreateResultRow}
    />
  );
}

function shoppingItemName(row: ShoppingListItemOkRow): string {
  return row.product_name ?? "(note)";
}

function ShoppingListItemsAddResultRow({ row }: { row: ShoppingListItemOkRow }) {
  return (
    <Group gap={4}>
      <PreviewText span fw={600}>
        {shoppingItemName(row)}
      </PreviewText>
      <PreviewText span c="dimmed">
        ×
      </PreviewText>
      <PreviewText span>
        {row.amount}
        {row.qu_name ? ` ${row.qu_name}` : ""}
      </PreviewText>
    </Group>
  );
}

function ShoppingListItemsAddResultView({
  result,
  variant,
}: ResultPreviewProps<z.infer<typeof zShoppingListItemsAddResult>>) {
  return (
    <BatchResultView
      rows={result}
      variant={variant}
      verb="added"
      rowName={shoppingItemName}
      RowView={ShoppingListItemsAddResultRow}
    />
  );
}

function StockEntryEditResultRow({ row }: { row: StockEntryEditOkRow }) {
  return (
    <Stack gap={2}>
      <Group gap={6}>
        <PreviewText span fw={600}>
          {row.entry.product_name}
        </PreviewText>
        <PreviewText span>
          {row.entry.amount} {row.entry.qu_name}
        </PreviewText>
        <PreviewText span c="dimmed">
          in {row.entry.location_name} · entry #{row.entry.entry_id}
        </PreviewText>
        {row.entry.open && <PreviewBadge variant="outline">opened</PreviewBadge>}
      </Group>
      {row.changes && (
        <PreviewText c="dimmed">
          Changed:{" "}
          {Object.keys(row.changes)
            .map((field) => field.replaceAll("_", " "))
            .join(", ")}
        </PreviewText>
      )}
    </Stack>
  );
}

function StockEntryEditResultView({ result, variant }: ResultPreviewProps<z.infer<typeof zStockEntryEditResult>>) {
  return (
    <BatchResultView
      rows={result}
      variant={variant}
      verb="edited"
      rowName={(row) => row.entry.product_name}
      RowView={StockEntryEditResultRow}
    />
  );
}

function StockGetResultView({ result, variant }: ResultPreviewProps<z.infer<typeof zStockGetResult>>) {
  const rows = variant === "compact" ? result.slice(0, COMPACT_ITEM_LIMIT) : result;
  return (
    <Stack gap={4}>
      <PreviewTitle>
        {result.length} stock {result.length === 1 ? "item" : "items"}
      </PreviewTitle>
      {rows.map((row, i) => (
        <Group gap={6} key={i}>
          <PreviewText span fw={600}>
            {row.product_name}
          </PreviewText>
          <PreviewText span>
            {row.amount} {row.qu_name}
          </PreviewText>
          <PreviewText span c="dimmed">
            in {row.location_name}
          </PreviewText>
          {row.amount_opened > 0 && <PreviewBadge variant="outline">{row.amount_opened} opened</PreviewBadge>}
        </Group>
      ))}
      <MoreLine count={result.length - rows.length} />
    </Stack>
  );
}

function NamedRowsResultView({ result, variant }: ResultPreviewProps<z.infer<typeof zProductsListResult>>) {
  const rows = variant === "compact" ? result.slice(0, COMPACT_ITEM_LIMIT) : result;
  return (
    <Stack gap={2}>
      <PreviewTitle>{result.length} found</PreviewTitle>
      {rows.map((row) => (
        <PreviewText key={row.id}>
          {row.name}{" "}
          <PreviewText span c="dimmed">
            #{row.id}
          </PreviewText>
        </PreviewText>
      ))}
      <MoreLine count={result.length - rows.length} />
    </Stack>
  );
}

function QuantityUnitsResultView({ result, variant }: ResultPreviewProps<z.infer<typeof zQuantityUnitsListResult>>) {
  return <NamedRowsResultView result={result} variant={variant} />;
}

function ShoppingListGetResultView({ result, variant }: ResultPreviewProps<z.infer<typeof zShoppingListGetResult>>) {
  const rows = variant === "compact" ? result.items.slice(0, COMPACT_ITEM_LIMIT) : result.items;
  return (
    <Stack gap={4}>
      <PreviewTitle>
        {result.name} · {result.items.length} items
      </PreviewTitle>
      {rows.map((row) => (
        <Group gap={6} key={row.item_id}>
          <PreviewText span td={row.done ? "line-through" : undefined}>
            {row.product_name ?? row.note ?? "(note)"}
          </PreviewText>
          <PreviewText span c="dimmed">
            × {row.amount}
            {row.qu_name ? ` ${row.qu_name}` : ""} · #{row.item_id}
          </PreviewText>
        </Group>
      ))}
      <MoreLine count={result.items.length - rows.length} />
    </Stack>
  );
}

function SystemInfoResultView({ result }: ResultPreviewProps<z.infer<typeof zSystemInfoResult>>) {
  return (
    <Stack gap={2}>
      {Object.entries(result).map(([key, value]) => (
        <PreviewText key={key}>
          <PreviewText span c="dimmed">
            {key.replaceAll("_", " ")}:{" "}
          </PreviewText>
          {String(value)}
        </PreviewText>
      ))}
    </Stack>
  );
}

function ShoppingListItemsRemoveResultView({
  result,
  variant,
}: ResultPreviewProps<z.infer<typeof zShoppingListItemsRemoveResult>>) {
  return (
    <BatchResultView
      rows={result}
      variant={variant}
      verb="removed"
      rowName={shoppingItemName}
      RowView={ShoppingListItemsAddResultRow}
    />
  );
}

/** Per-tool result widgets for the `grocy-sf` server's list-returning batch tools. */
export const grocyResultPreviews = {
  stock_add: defineResultPreview(zStockAddResult, StockAddResultView),
  stock_entry_edit: defineResultPreview(zStockEntryEditResult, StockEntryEditResultView),
  stock_get: defineResultPreview(zStockGetResult, StockGetResultView),
  products_list: defineResultPreview(zProductsListResult, NamedRowsResultView),
  quantity_units_list: defineResultPreview(zQuantityUnitsListResult, QuantityUnitsResultView),
  get_system_info: defineResultPreview(zSystemInfoResult, SystemInfoResultView),
  shopping_list_get: defineResultPreview(zShoppingListGetResult, ShoppingListGetResultView),
  shopping_list_items_remove: defineResultPreview(zShoppingListItemsRemoveResult, ShoppingListItemsRemoveResultView),
  products_create: defineResultPreview(zProductsCreateResult, ProductsCreateResultView),
  shopping_list_items_add: defineResultPreview(zShoppingListItemsAddResult, ShoppingListItemsAddResultView),
} satisfies Record<string, ToolResultPreview>;
