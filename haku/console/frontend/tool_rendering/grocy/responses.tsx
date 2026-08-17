// Result rendering for the remote `grocy-sf` server's batch tools (the argument-side widgets
// live in ./requests.tsx). Each batch tool returns one row per input item — `kind: "ok"` with
// per-op details, or `kind: "error"` with an `error` message — so the widgets show an ok/failed
// count summary (compact adds the first few product names) and, detailed, every row with its
// amounts/units/locations, failed rows in red. The result schemas are the FastMCP-advertised
// output schemas (generated in mcp_tool_result_schema.ts from tools/list). The two tools with no
// reliable generated schema — `shopping_list_get` (returns a bare dict → empty output schema) and
// `get_system_info` (an OpenAPI tool with no batch counterpart, not in the catalog) — keep
// hand-authored schemas at the bottom.

import { Group, Stack } from "@mantine/core";
import type { ReactNode } from "react";
import { z } from "zod";

import { mcpToolResultSchema } from "../../mcp_tool_result_schema";
import {
  COMPACT_ITEM_LIMIT,
  MoreLine,
  PreviewBadge,
  PreviewText,
  PreviewTitle,
  type PreviewVariant,
} from "../vocabulary";
import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry";
import { GROCY_SERVER_ID } from "../server_ids";

// A batch result is a list of per-item rows: an ok variant (per-op details) or an error variant
// (an `error` message). FastMCP emits the union as `anyOf`; both variants carry an optional `kind`
// default, so split on `error` presence rather than the `kind` discriminant. The ok row for a given
// tool is the array element without an `error` field.
type ErrorRow = { error: string };
type OkRowOf<Result> = Result extends (infer Element)[] ? Exclude<Element, ErrorRow> : never;

const zStockAddResult = mcpToolResultSchema(GROCY_SERVER_ID, "stock_add");
const zProductsCreateResult = mcpToolResultSchema(GROCY_SERVER_ID, "products_create");
const zShoppingListItemsAddResult = mcpToolResultSchema(GROCY_SERVER_ID, "shopping_list_items_add");
const zShoppingListItemsRemoveResult = mcpToolResultSchema(GROCY_SERVER_ID, "shopping_list_items_remove");
const zStockEntryEditResult = mcpToolResultSchema(GROCY_SERVER_ID, "stock_entry_edit");
const zStockGetResult = mcpToolResultSchema(GROCY_SERVER_ID, "stock_get");
const zProductsListResult = mcpToolResultSchema(GROCY_SERVER_ID, "products_list");
const zQuantityUnitsListResult = mcpToolResultSchema(GROCY_SERVER_ID, "quantity_units_list");

type StockAddOkRow = OkRowOf<z.infer<typeof zStockAddResult>>;
type ProductsCreateOkRow = OkRowOf<z.infer<typeof zProductsCreateResult>>;
type ShoppingListItemOkRow = OkRowOf<z.infer<typeof zShoppingListItemsAddResult>>;
type StockEntryEditOkRow = OkRowOf<z.infer<typeof zStockEntryEditResult>>;

// These two have no reliable generated result schema, so they stay hand-authored (see header).
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
// Grocy's `GET /system/info` nests the app version in its own object ({Version, ReleaseDate, …})
// rather than a bare string, typed explicitly so the widget formats it instead of falling through
// to a blind `String(value)` that prints "[object Object]".
const zSystemInfoResult = z.looseObject({
  grocy_version: z.looseObject({ Version: z.string(), ReleaseDate: z.string().nullish() }).nullish(),
  php_version: z.string().nullish(),
  sqlite_version: z.string().nullish(),
  db_version: z.union([z.string(), z.number()]).nullish(),
  os: z.string().nullish(),
  client: z.string().nullish(),
});

function splitRows<Ok extends object>(rows: readonly (Ok | ErrorRow)[]): { ok: Ok[]; failed: ErrorRow[] } {
  const ok: Ok[] = [];
  const failed: ErrorRow[] = [];
  for (const row of rows) {
    // An error row carries an `error` field; an ok row never does (see grocy_mcp.mcp_types). The
    // `in` check picks the branch at runtime; both sides are cast because a generic `Ok` can't be
    // narrowed away from `ErrorRow` structurally.
    if ("error" in row) failed.push(row as ErrorRow);
    else ok.push(row as Ok);
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

/** Shared shape of the batch-result widgets: the count summary, then compact's first few row names
 * (+ "… +N more") or detailed's full per-row lines with failed rows in red. */
function BatchResultView<Ok extends object>({
  rows,
  variant,
  verb,
  rowName,
  RowView,
}: {
  rows: readonly (Ok | ErrorRow)[];
  variant: PreviewVariant;
  verb: string;
  rowName: (row: Ok) => string;
  RowView: (props: { row: Ok }) => ReactNode;
}) {
  const { ok, failed } = splitRows<Ok>(rows);
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

// CreateOk carries only `created_object_id` (no name), so a created row is identified by its id.
function productsCreateRowName(row: ProductsCreateOkRow): string {
  return row.created_object_id != null ? `product #${row.created_object_id}` : "product";
}

function ProductsCreateResultRow({ row }: { row: ProductsCreateOkRow }) {
  return (
    <Group gap={6}>
      <PreviewText span fw={600}>
        {productsCreateRowName(row)}
      </PreviewText>
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

// products_list and quantity_units_list both return rows shaped `{id, name}` (a brief/full union);
// either's result is assignable here, so the view is decoupled from one specific schema.
function NamedRowsResultView({ result, variant }: ResultPreviewProps<{ id: number; name: string }[]>) {
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

function SystemInfoRow({ label, value }: { label: string; value: string }) {
  return (
    <PreviewText>
      <PreviewText span c="dimmed">
        {label}:{" "}
      </PreviewText>
      {value}
    </PreviewText>
  );
}

function SystemInfoResultView({ result }: ResultPreviewProps<z.infer<typeof zSystemInfoResult>>) {
  const rows: { key: string; label: string; value: string }[] = [];
  if (result.grocy_version) {
    rows.push({
      key: "grocy_version",
      label: "grocy version",
      value: result.grocy_version.ReleaseDate
        ? `${result.grocy_version.Version} (${result.grocy_version.ReleaseDate})`
        : result.grocy_version.Version,
    });
  }
  if (result.php_version) rows.push({ key: "php_version", label: "php version", value: result.php_version });
  if (result.sqlite_version) {
    rows.push({ key: "sqlite_version", label: "sqlite version", value: result.sqlite_version });
  }
  if (result.db_version != null)
    rows.push({ key: "db_version", label: "db version", value: String(result.db_version) });
  if (result.os) rows.push({ key: "os", label: "os", value: result.os });
  if (result.client) rows.push({ key: "client", label: "client", value: result.client });
  return (
    <Stack gap={2}>
      {rows.map((row) => (
        <SystemInfoRow key={row.key} label={row.label} value={row.value} />
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
