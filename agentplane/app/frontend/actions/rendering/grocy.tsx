import { Stack, Text } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { Chip } from "./chips";
import { definePreview, type ArgumentsPreview } from "./entry";
import { defineResultPreview, type ResultPreview } from "./result_entry";

const listArguments = z.strictObject({ detail: z.enum(["brief", "full"]).optional() });

function ListOptions({ args, noun }: { args: z.infer<typeof listArguments>; noun: string }): JSX.Element {
  const detail = args.detail ?? "brief";
  return <Text size="sm">{detail === "full" ? `Full ${noun} records` : `${noun} names`}</Text>;
}

function ProductListOptions({ args }: { args: z.infer<typeof listArguments> }): JSX.Element {
  return <ListOptions args={args} noun="Product" />;
}

function QuantityUnitListOptions({ args }: { args: z.infer<typeof listArguments> }): JSX.Element {
  return <ListOptions args={args} noun="Quantity unit" />;
}

export const productsListArguments: ArgumentsPreview = definePreview(listArguments, ProductListOptions);
export const quantityUnitsListArguments: ArgumentsPreview = definePreview(listArguments, QuantityUnitListOptions);

const noArguments = z.strictObject({});

function SystemInfoRequest(): JSX.Element {
  return <Text size="sm">Grocy server version and system details</Text>;
}

export const systemInfoArguments: ArgumentsPreview = definePreview(noArguments, SystemInfoRequest);

const namedRows = z.array(z.object({ id: z.number(), name: z.string() }).passthrough());

function NamedRows({ result }: { result: z.infer<typeof namedRows> }): JSX.Element {
  return (
    <Stack gap={4}>
      <Text fw={600}>{result.length} found</Text>
      {result.map((row) => (
        <Text key={row.id} size="sm">
          {row.name}{" "}
          <Text span c="dimmed">
            #{row.id}
          </Text>
        </Text>
      ))}
    </Stack>
  );
}

export const productsListResult: ResultPreview = defineResultPreview(namedRows, NamedRows);
export const quantityUnitsListResult: ResultPreview = defineResultPreview(namedRows, NamedRows);

const systemInfo = z
  .object({
    grocy_version: z
      .object({ Version: z.string(), ReleaseDate: z.string().nullable().optional() })
      .nullable()
      .optional(),
    php_version: z.string().nullable().optional(),
    sqlite_version: z.string().nullable().optional(),
    db_version: z.union([z.string(), z.number()]).nullable().optional(),
    os: z.string().nullable().optional(),
    client: z.string().nullable().optional(),
  })
  .passthrough();

function SystemInfo({ result }: { result: z.infer<typeof systemInfo> }): JSX.Element {
  const fields: Array<[string, string]> = [];
  if (result.grocy_version) {
    fields.push([
      "Grocy version",
      result.grocy_version.ReleaseDate
        ? `${result.grocy_version.Version} (${result.grocy_version.ReleaseDate})`
        : result.grocy_version.Version,
    ]);
  }
  if (result.php_version) fields.push(["PHP version", result.php_version]);
  if (result.sqlite_version) fields.push(["SQLite version", result.sqlite_version]);
  if (result.db_version != null) fields.push(["Database version", String(result.db_version)]);
  if (result.os) fields.push(["OS", result.os]);
  if (result.client) fields.push(["Client", result.client]);
  return (
    <Stack gap="xs">
      {fields.map(([label, value]) => (
        <Chip key={label} label={label} value={value} />
      ))}
      {fields.length === 0 && (
        <Text size="sm" c="dimmed">
          No system details returned.
        </Text>
      )}
    </Stack>
  );
}

export const systemInfoResult: ResultPreview = defineResultPreview(systemInfo, SystemInfo);

const errorRow = z.object({ error: z.string() }).passthrough();
const stockAddOk = z
  .object({
    kind: z.literal("ok").optional(),
    product_name: z.string(),
    amount_delta: z.number().nullable().optional(),
    new_amount: z.number().nullable().optional(),
    qu_name: z.string(),
    location_name: z.string(),
    best_before_date: z.string().nullable().optional(),
  })
  .passthrough();
const stockAddResultSchema = z.array(z.union([errorRow, stockAddOk]));

function isErrorRow(value: unknown): value is { error: string } {
  return typeof value === "object" && value !== null && "error" in value && typeof value.error === "string";
}

function BatchRows<Ok extends object>({
  rows,
  verb,
  children,
}: {
  rows: unknown[];
  verb: string;
  children: (row: Ok, index: number) => JSX.Element;
}): JSX.Element {
  const ok: Ok[] = [];
  const failed: Array<{ error: string }> = [];
  for (const row of rows) {
    if (isErrorRow(row)) failed.push(row);
    else ok.push(row as Ok);
  }
  return (
    <Stack gap="xs">
      <Text size="sm" fw={600}>
        {ok.length} {verb}
        {failed.length > 0 ? ` · ${failed.length} failed` : ""}
      </Text>
      {ok.map(children)}
      {failed.map((row, index) => (
        <Text key={`error-${index}`} size="sm" c="red">
          ✗ {row.error}
        </Text>
      ))}
    </Stack>
  );
}

function StockAddResult({ result }: { result: z.infer<typeof stockAddResultSchema> }): JSX.Element {
  return (
    <BatchRows<z.infer<typeof stockAddOk>>
      rows={result}
      verb="added"
      children={(row, index) => (
        <Stack key={index} gap={2}>
          <Chip label="Product" value={row.product_name} />
          {row.amount_delta != null && <Chip label="Added" value={`${row.amount_delta} ${row.qu_name}`} />}
          <Chip label="Location" value={row.location_name} />
          {row.new_amount != null && <Chip label="New total" value={`${row.new_amount} ${row.qu_name}`} />}
          {row.best_before_date && <Chip label="Best before" value={row.best_before_date} />}
        </Stack>
      )}
    />
  );
}

export const stockAddResult: ResultPreview = defineResultPreview(stockAddResultSchema, StockAddResult);

const productsCreateOk = z
  .object({ kind: z.literal("ok").optional(), created_object_id: z.number().nullable().optional() })
  .passthrough();
const productsCreateResultSchema = z.array(z.union([errorRow, productsCreateOk]));

function ProductsCreateResult({ result }: { result: z.infer<typeof productsCreateResultSchema> }): JSX.Element {
  return (
    <BatchRows<z.infer<typeof productsCreateOk>>
      rows={result}
      verb="created"
      children={(row, index) => (
        <Chip
          key={index}
          label="Product"
          value={row.created_object_id == null ? "created" : `#${row.created_object_id}`}
        />
      )}
    />
  );
}

export const productsCreateResult: ResultPreview = defineResultPreview(
  productsCreateResultSchema,
  ProductsCreateResult
);

const stockGetResultSchema = z.array(
  z
    .object({
      product_name: z.string(),
      amount: z.number(),
      amount_opened: z.number().optional(),
      qu_name: z.string(),
      location_name: z.string(),
    })
    .passthrough()
);

function StockGetResult({ result }: { result: z.infer<typeof stockGetResultSchema> }): JSX.Element {
  return (
    <Stack gap="xs">
      <Text size="sm" fw={600}>
        {result.length} stock {result.length === 1 ? "item" : "items"}
      </Text>
      {result.map((row, index) => (
        <Stack key={index} gap={2}>
          <Chip label="Product" value={row.product_name} />
          <Chip label="Amount" value={`${row.amount} ${row.qu_name}`} />
          <Chip label="Location" value={row.location_name} />
          {row.amount_opened != null && row.amount_opened > 0 && <Chip label="Opened" value={row.amount_opened} />}
        </Stack>
      ))}
    </Stack>
  );
}

export const stockGetResult: ResultPreview = defineResultPreview(stockGetResultSchema, StockGetResult);

const stockEntryEditOk = z
  .object({
    kind: z.literal("ok").optional(),
    entry: z
      .object({
        entry_id: z.number(),
        product_name: z.string(),
        amount: z.number(),
        qu_name: z.string(),
        location_name: z.string(),
        open: z.boolean().optional(),
      })
      .passthrough(),
    changes: z.record(z.string(), z.unknown()).nullable().optional(),
  })
  .passthrough();
const stockEntryEditError = errorRow;
const stockEntryEditResultSchema = z.array(z.union([stockEntryEditError, stockEntryEditOk]));

function StockEntryEditResult({ result }: { result: z.infer<typeof stockEntryEditResultSchema> }): JSX.Element {
  return (
    <BatchRows<z.infer<typeof stockEntryEditOk>>
      // The batch schema has already separated valid success shapes from errors.
      rows={result}
      verb="edited"
      children={(row, index) => (
        <Stack key={index} gap={2}>
          <Chip label="Product" value={row.entry.product_name} />
          <Chip label="Amount" value={`${row.entry.amount} ${row.entry.qu_name}`} />
          <Chip label="Location" value={row.entry.location_name} />
          <Chip label="Entry" value={`#${row.entry.entry_id}`} />
          {row.entry.open && <Chip label="Status" value="opened" />}
          {row.changes && (
            <Chip
              label="Changed fields"
              value={Object.keys(row.changes)
                .map((key) => key.replaceAll("_", " "))
                .join(", ")}
            />
          )}
        </Stack>
      )}
    />
  );
}

export const stockEntryEditResult: ResultPreview = defineResultPreview(
  stockEntryEditResultSchema,
  StockEntryEditResult
);

const shoppingListMutationOk = z
  .object({
    kind: z.literal("ok").optional(),
    item_id: z.number(),
    product_name: z.string().nullable().optional(),
    amount: z.number(),
    qu_name: z.string().nullable().optional(),
    note: z.string().nullable().optional(),
  })
  .passthrough();
const shoppingListMutationResultSchema = z.array(z.union([errorRow, shoppingListMutationOk]));

function ShoppingListMutationResult({
  result,
  verb,
}: {
  result: z.infer<typeof shoppingListMutationResultSchema>;
  verb: string;
}): JSX.Element {
  return (
    <BatchRows<z.infer<typeof shoppingListMutationOk>>
      // The batch schema has already separated valid success shapes from errors.
      rows={result}
      verb={verb}
      children={(row, index) => (
        <Stack key={index} gap={2}>
          <Chip label="Item" value={row.product_name ?? row.note ?? "(note)"} />
          <Chip label="Amount" value={`${row.amount}${row.qu_name ? ` ${row.qu_name}` : ""}`} />
          <Chip label="Item ID" value={row.item_id} />
        </Stack>
      )}
    />
  );
}

function ShoppingListItemsAddResult({
  result,
}: {
  result: z.infer<typeof shoppingListMutationResultSchema>;
}): JSX.Element {
  return <ShoppingListMutationResult result={result} verb="added" />;
}

function ShoppingListItemsRemoveResult({
  result,
}: {
  result: z.infer<typeof shoppingListMutationResultSchema>;
}): JSX.Element {
  return <ShoppingListMutationResult result={result} verb="removed" />;
}

export const shoppingListItemsAddResult: ResultPreview = defineResultPreview(
  shoppingListMutationResultSchema,
  ShoppingListItemsAddResult
);
export const shoppingListItemsRemoveResult: ResultPreview = defineResultPreview(
  shoppingListMutationResultSchema,
  ShoppingListItemsRemoveResult
);

const shoppingListResultSchema = z
  .object({
    name: z.string(),
    items: z.array(
      z
        .object({
          item_id: z.number(),
          product_name: z.string().nullable().optional(),
          amount: z.number(),
          qu_name: z.string().nullable().optional(),
          note: z.string().nullable().optional(),
          done: z.boolean(),
        })
        .passthrough()
    ),
  })
  .passthrough();

function ShoppingListResult({ result }: { result: z.infer<typeof shoppingListResultSchema> }): JSX.Element {
  return (
    <Stack gap="xs">
      <Text size="sm" fw={600}>
        {result.name} · {result.items.length} items
      </Text>
      {result.items.map((item) => (
        <Stack key={item.item_id} gap={2}>
          <Chip label="Item" value={item.product_name ?? item.note ?? "(note)"} />
          <Chip label="Amount" value={`${item.amount}${item.qu_name ? ` ${item.qu_name}` : ""}`} />
          <Chip label="Status" value={item.done ? "done" : "open"} />
          <Chip label="Item ID" value={item.item_id} />
        </Stack>
      ))}
    </Stack>
  );
}

export const shoppingListResult: ResultPreview = defineResultPreview(shoppingListResultSchema, ShoppingListResult);
