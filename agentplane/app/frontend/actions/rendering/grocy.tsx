import { Stack } from "@mantine/core";
import type { JSX, ReactNode } from "react";
import { z } from "zod";

import { defineCallPreview, definePreview } from "./entry";
import { type ActionPresentation, expandsForRejectedArguments } from "./presentation";
import { defineResultPreview, type ResultPreview } from "./result_entry";
import { batchError, batchOutcome, displayRef, Line, objectLink, reference } from "./grocy_shared";
import { grocyCatalogRenderers } from "./grocy_catalog";
import { grocyShoppingRenderers } from "./grocy_shopping";
import { grocyStockRenderers } from "./grocy_stock";

const listArguments = z.strictObject({ detail: z.enum(["brief", "full"]).optional() });
const noArguments = z.strictObject({});
const namedRow = z.object({ id: z.number().int(), name: z.string() });

const productsListResult = defineResultPreview(
  z.array(
    namedRow.extend({
      min_stock_amount: z.number().optional(),
      description: z.string().nullish(),
    })
  ),
  ({ result }) => (
    <Stack gap={4}>
      <Line>
        {result.length === 0 ? "No products" : `${result.length} ${result.length === 1 ? "product" : "products"}`}
      </Line>
      {result.map((row) => (
        <Line key={row.id}>
          {objectLink("product", row.id, row.name)}
          {row.min_stock_amount !== undefined &&
            row.min_stock_amount > 0 &&
            ` · Minimum ${row.min_stock_amount} in stock`}
          {row.description && ` · ${row.description}`}
        </Line>
      ))}
    </Stack>
  )
);

const quantityUnitsListResult = defineResultPreview(
  z.array(namedRow.extend({ name_plural: z.string().nullish(), description: z.string().nullish() })),
  ({ result }) => (
    <Stack gap={4}>
      <Line>
        {result.length === 0
          ? "No quantity units"
          : `${result.length} quantity ${result.length === 1 ? "unit" : "units"}`}
      </Line>
      {result.map((row) => (
        <Line key={row.id}>
          {objectLink("quantityunit", row.id, row.name)}
          {row.name_plural && row.name_plural !== row.name && ` · Plural: ${row.name_plural}`}
          {row.description && ` · ${row.description}`}
        </Line>
      ))}
    </Stack>
  )
);

const systemInfo = z.object({
  grocy_version: z.object({ Version: z.string(), ReleaseDate: z.string().nullish() }).nullish(),
  php_version: z.string().nullish(),
  sqlite_version: z.string().nullish(),
  db_version: z.union([z.string(), z.number()]).nullish(),
  os: z.string().nullish(),
  client: z.string().nullish(),
});

const systemInfoResult = defineResultPreview(systemInfo, ({ result }) => {
  const diagnostics = [
    result.php_version && `PHP ${result.php_version}`,
    result.sqlite_version && `SQLite ${result.sqlite_version}`,
    result.db_version != null && `Database ${result.db_version}`,
  ].filter(Boolean);
  return (
    <Stack gap="xs">
      {result.grocy_version && (
        <Line>
          <strong>Grocy {result.grocy_version.Version}</strong>
          {result.grocy_version.ReleaseDate && ` · Released ${result.grocy_version.ReleaseDate}`}
        </Line>
      )}
      {result.os && <Line>Operating system · {result.os}</Line>}
      {result.client && <Line>Client · {result.client}</Line>}
      {diagnostics.length > 0 && (
        <details>
          <summary>Dependency versions</summary>
          {diagnostics.map((item) => (
            <Line key={String(item)}>{item}</Line>
          ))}
        </details>
      )}
      {!result.grocy_version && !result.os && !result.client && diagnostics.length === 0 && (
        <Line>No system details returned.</Line>
      )}
    </Stack>
  );
});

function listPresentation(label: string, schema: z.ZodType, result: ResultPreview): ActionPresentation {
  const noAdditionalArguments = definePreview(schema, () => null);
  return {
    fallbackLabel: label,
    pane: { opened: noAdditionalArguments },
    details: { arguments: noAdditionalArguments, result },
    offersCardDecisionButtons: (args) => schema.safeParse(args).success,
    compactCardExpands: expandsForRejectedArguments(schema),
  };
}

const editField = z.enum(["description", "product_group", "parent_product", "calories"]);
const editProduct = z.strictObject({
  product: reference,
  name: z.string().nullish(),
  stock_qu: reference.nullish(),
  location: reference.nullish(),
  purchase_qu: reference.nullish(),
  consume_qu: reference.nullish(),
  min_stock_amount: z.number().nullish(),
  default_best_before_days: z.number().int().nullish(),
  due_type: z.union([z.literal(1), z.literal(2)]).nullish(),
  parent_product: reference.nullish(),
  product_group: reference.nullish(),
  description: z.string().nullish(),
  clear_fields: z.array(editField).nullish(),
});
const editProductArguments = z.strictObject({ items: z.array(editProduct) });
const defaultShelfLife = (days: number): string =>
  days === -1 ? "Never expires" : days === 0 ? "Due today" : `${days} ${days === 1 ? "day" : "days"}`;
const clearLabels: Record<z.infer<typeof editField>, string> = {
  description: "Description",
  product_group: "Product group",
  parent_product: "Parent product",
  calories: "Calories",
};

function editChanges(item: z.infer<typeof editProduct>): [string, ReactNode][] {
  const clears = new Set(item.clear_fields);
  const fields: [string, ReactNode][] = [
    ["Name", item.name],
    ["Default location", item.location != null && displayRef(item.location)],
    ["Stock unit", item.stock_qu != null && displayRef(item.stock_qu)],
    ["Purchase unit", item.purchase_qu != null && displayRef(item.purchase_qu)],
    ["Consume unit", item.consume_qu != null && displayRef(item.consume_qu)],
    [
      "Minimum stock",
      item.min_stock_amount != null &&
        (item.min_stock_amount === 0 ? "Disable low-stock threshold" : `${item.min_stock_amount} stock units`),
    ],
    ["Default shelf life", item.default_best_before_days != null && defaultShelfLife(item.default_best_before_days)],
    ["Due date means", item.due_type != null && (item.due_type === 2 ? "Expiration" : "Best before")],
    ["Parent product", !clears.has("parent_product") && item.parent_product != null && displayRef(item.parent_product)],
    ["Product group", !clears.has("product_group") && item.product_group != null && displayRef(item.product_group)],
    ["Description", !clears.has("description") ? item.description : undefined],
  ];
  return [
    ...fields.filter(([, value]) => value !== false && value != null),
    ...[...clears].map((key): [string, string] => [clearLabels[key], "Clear"]),
  ];
}

function ProductChanges({ args }: { args: z.infer<typeof editProductArguments> }): JSX.Element {
  return (
    <Stack gap="sm">
      {args.items.length === 0 && <Line>No product edits submitted</Line>}
      {args.items.map((item, index) => {
        const changes = editChanges(item);
        return (
          <Stack gap={2} key={index}>
            <Line>
              <strong>
                {typeof item.product === "number"
                  ? objectLink("product", item.product, `Product #${item.product}`)
                  : item.product}
              </strong>
            </Line>
            {changes.map(([label, value]) => (
              <Line key={label}>
                {label} · {value === "" ? "(empty text)" : value}
              </Line>
            ))}
            {changes.length === 0 && <Line>No field changes</Line>}
          </Stack>
        );
      })}
    </Stack>
  );
}

const editProductResult = z.array(
  z.union([z.strictObject({ kind: z.literal("ok"), object_id: z.number().int() }), batchError])
);

function editedProductsLabel(rows: z.infer<typeof editProductResult>): string {
  const count = rows.filter((row) => row.kind === "ok").length;
  return `${count} ${count === 1 ? "product" : "products"} edited`;
}

const editProductReceipt = defineResultPreview(editProductResult, ({ result }) => (
  <Stack gap={4}>
    <Line>{editedProductsLabel(result)}</Line>
    {result.map((row, index) => (
      <Line key={index}>
        {row.kind === "ok" ? objectLink("product", row.object_id, "Open edited product") : <>Failed · {row.error}</>}
      </Line>
    ))}
  </Stack>
));

const editProductCall = defineCallPreview(editProductArguments, editProductResult, ({ args, result }) => {
  if (!result) return <ProductChanges args={args} />;
  const sameItems = result.length === args.items.length;
  return (
    <Stack gap="sm">
      <ProductChanges args={args} />
      <Stack gap={4}>
        <Line>
          <strong>{editedProductsLabel(result)}</strong>
        </Line>
        {result.map((row, index) => {
          const item = sameItems ? args.items[index] : undefined;
          const name = typeof item?.product === "string" ? item.product : undefined;
          return (
            <Line key={index}>
              {row.kind === "ok" ? (
                objectLink("product", row.object_id, item?.name ?? name ?? "Open edited product")
              ) : (
                <>
                  Failed{name && ` · ${name}`} · {row.error}
                </>
              )}
            </Line>
          );
        })}
      </Stack>
    </Stack>
  );
});

const edits = definePreview(editProductArguments, ProductChanges);

const stockGetArguments = z.strictObject({
  products: z.array(reference).optional(),
  locations: z.array(reference).optional(),
});
const scopedStock = definePreview(stockGetArguments, ({ args }) => {
  const filters = [
    args.products && args.products.length > 0 && (
      <>
        Products ·{" "}
        {args.products.map((value, i) => (
          <span key={i}>
            {i > 0 && ", "}
            {displayRef(value)}
          </span>
        ))}
      </>
    ),
    args.locations && args.locations.length > 0 && (
      <>
        Locations ·{" "}
        {args.locations.map((value, i) => (
          <span key={i}>
            {i > 0 && ", "}
            {displayRef(value)}
          </span>
        ))}
      </>
    ),
  ].filter(Boolean);
  return (
    <Stack gap={4}>
      {filters.length === 0 ? (
        <Line>All products and locations</Line>
      ) : (
        filters.map((filter, index) => <Line key={index}>{filter}</Line>)
      )}
    </Stack>
  );
});

const stockGetResult = defineResultPreview(
  z.array(
    z.strictObject({
      product_id: z.number().int(),
      product_name: z.string(),
      amount: z.number(),
      amount_opened: z.number(),
      qu_name: z.string(),
      location_name: z.string(),
      best_before_date: z.string().nullish(),
    })
  ),
  ({ result }) => (
    <Stack gap="xs">
      <Line>
        {result.length === 0 ? "No stock matched" : `${result.length} stock ${result.length === 1 ? "item" : "items"}`}
      </Line>
      {result.map((item, index) => (
        <Stack key={index} gap={2}>
          <Line>
            <strong>{objectLink("product", item.product_id, item.product_name)}</strong> · {item.amount} {item.qu_name}{" "}
            in {item.location_name}
          </Line>
          {(item.amount_opened > 0 || item.best_before_date) && (
            <Line>
              {item.amount_opened > 0 && `${item.amount_opened} opened`}
              {item.amount_opened > 0 && item.best_before_date && " · "}
              {item.best_before_date && `Best before ${item.best_before_date}`}
            </Line>
          )}
        </Stack>
      ))}
    </Stack>
  )
);

export const grocyRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ...grocyStockRenderers,
  ...grocyShoppingRenderers,
  ...grocyCatalogRenderers,
  ["products_list", listPresentation("List Grocy products", listArguments, productsListResult)],
  ["quantity_units_list", listPresentation("List quantity units", listArguments, quantityUnitsListResult)],
  ["get_system_info", listPresentation("Show Grocy system information", noArguments, systemInfoResult)],
  [
    "products_edit",
    {
      fallbackLabel: "Edit Grocy products",
      call: editProductCall,
      pane: { opened: edits },
      details: { arguments: edits, result: editProductReceipt },
      outcome: (result) => {
        const parsed = editProductResult.safeParse(result);
        return parsed.success ? batchOutcome(parsed.data) : undefined;
      },
    },
  ],
  [
    "stock_get",
    {
      fallbackLabel: "View Grocy stock on hand",
      pane: { collapsed: scopedStock },
      details: { arguments: scopedStock, result: stockGetResult },
      compactCardExpands: expandsForRejectedArguments(stockGetArguments),
    },
  ],
]);
