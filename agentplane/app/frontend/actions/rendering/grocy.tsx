import { Code, Stack } from "@mantine/core";
import type { JSX, ReactNode } from "react";
import { z } from "zod";

import type { GrocyReferenceSummary } from "../../../../action_service/api/contracts/operator_action_service_pb";

import { definePreview, defineReceiptPreview, type PreviewProps } from "./entry";
import { type ActionPresentation, type CardDecisionProps, expandsForRejectedArguments } from "./presentation";
import { defineResultPreview, type ResultPreview } from "./result_entry";
import {
  batchError,
  batchOutcome,
  displayRef,
  GrocyReview,
  type GrocyReferenceKind,
  Line,
  objectLink,
  reference,
  useGrocyReferences,
  useGrocyRequestReferences,
} from "./grocy_shared";
import { grocyCatalogRenderers } from "./grocy_catalog";
import { grocyEntityRenderers } from "./grocy_entities";
import { grocyShoppingRenderers } from "./grocy_shopping";
import { grocyStockRenderers } from "./grocy_stock";

const listArguments = z.strictObject({ detail: z.enum(["brief", "full"]).optional() });
const noArguments = z.strictObject({});
const namedRow = z.object({ id: z.number().int(), name: z.string() });

const productsListResult = defineResultPreview(
  z.array(
    namedRow.extend({
      min_stock_amount: z
        .union([
          z.number(),
          z
            .string()
            .regex(/^\d+(?:\.\d+)?$/)
            .transform(Number),
        ])
        .optional(),
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

function oldReference(id: bigint | undefined, kind: GrocyReferenceKind, optional = false): ReactNode {
  if (id == null || id <= 0n) return optional ? "None" : undefined;
  const value = Number(id);
  return Number.isSafeInteger(value) && value > 0 ? displayRef(value, kind) : undefined;
}

function oldText(text: string | undefined): string {
  return text === undefined ? "None" : text || "(empty text)";
}

function editChanges(
  item: z.infer<typeof editProduct>,
  summary?: GrocyReferenceSummary
): [string, ReactNode, ReactNode][] {
  const clears = new Set(item.clear_fields);
  const current = summary?.productEditCurrent;
  const fields: [string, ReactNode, ReactNode][] = [
    ["Name", item.name, summary?.name || undefined],
    [
      "Default location",
      item.location != null && displayRef(item.location, "locations"),
      oldReference(current?.locationId, "locations"),
    ],
    [
      "Stock unit",
      item.stock_qu != null && displayRef(item.stock_qu, "quantity_units"),
      oldReference(current?.stockUnitId, "quantity_units"),
    ],
    [
      "Purchase unit",
      item.purchase_qu != null && displayRef(item.purchase_qu, "quantity_units"),
      oldReference(current?.purchaseUnitId, "quantity_units"),
    ],
    [
      "Consume unit",
      item.consume_qu != null && displayRef(item.consume_qu, "quantity_units"),
      oldReference(current?.consumeUnitId, "quantity_units"),
    ],
    [
      "Minimum stock",
      item.min_stock_amount != null &&
        (item.min_stock_amount === 0 ? "Disable low-stock threshold" : `${item.min_stock_amount} stock units`),
      current?.minimumStock === undefined ? undefined : `${current.minimumStock} stock units`,
    ],
    [
      "Default shelf life",
      item.default_best_before_days != null && defaultShelfLife(item.default_best_before_days),
      current?.shelfLifeDays === undefined ? undefined : defaultShelfLife(Number(current.shelfLifeDays)),
    ],
    [
      "Due date means",
      item.due_type != null && (item.due_type === 2 ? "Expiration" : "Best before"),
      current?.dueType === 2n ? "Expiration" : current?.dueType === 1n ? "Best before" : undefined,
    ],
    [
      "Parent product",
      !clears.has("parent_product") && item.parent_product != null && displayRef(item.parent_product, "products"),
      current && oldReference(current.parentProductId, "products", true),
    ],
    [
      "Product group",
      !clears.has("product_group") && item.product_group != null && displayRef(item.product_group, "product_groups"),
      current && oldReference(current.productGroupId, "product_groups", true),
    ],
    ["Description", !clears.has("description") ? item.description : undefined, current && oldText(current.description)],
  ];
  const clearValues: Record<keyof typeof clearLabels, ReactNode> = {
    description: current && oldText(current.description),
    product_group: current && oldReference(current.productGroupId, "product_groups", true),
    parent_product: current && oldReference(current.parentProductId, "products", true),
    calories: current && (current.calories ?? "None"),
  };
  return [
    ...fields.filter(([, value]) => value !== false && value != null),
    ...[...clears].map((key): [string, string, ReactNode] => [clearLabels[key], "Clear", clearValues[key]]),
  ];
}

const editProductResult = z.array(
  z.union([z.strictObject({ kind: z.literal("ok"), object_id: z.number().int() }), batchError])
);

function recordedRename(
  item: z.infer<typeof editProduct> | undefined,
  row: z.infer<typeof editProductResult>[number] | undefined
): string | undefined {
  return row?.kind === "ok" && item?.product === row.object_id && item.name?.trim() ? item.name : undefined;
}

function ProductChanges({
  args,
  result,
}: {
  args: z.infer<typeof editProductArguments>;
  result?: z.infer<typeof editProductResult>;
}): JSX.Element {
  const refs = useGrocyReferences();
  return (
    <Stack gap="sm">
      {args.items.length === 0 && <Line>No product edits submitted</Line>}
      {args.items.map((item, index) => {
        const summary = typeof item.product === "number" ? refs?.get(`product_edits/${item.product}`) : undefined;
        const changes = editChanges(item, summary);
        const title = summary?.name || recordedRename(item, result?.[index]);
        return (
          <Stack gap={2} key={index}>
            <Line>
              <strong>
                {typeof item.product === "number" && title
                  ? objectLink("product", item.product, title)
                  : displayRef(item.product, "products")}
              </strong>
              {summary?.name && (
                <>
                  {" "}
                  · <Code>#{item.product}</Code>
                </>
              )}
            </Line>
            {changes.map(([label, value, old]) => (
              <Line key={label}>
                {label} · {old != null && <>{old} → </>}
                {value === "" ? "(empty text)" : value}
              </Line>
            ))}
            {changes.length === 0 && <Line>No field changes</Line>}
          </Stack>
        );
      })}
    </Stack>
  );
}

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

const editProductCall = defineReceiptPreview(editProductArguments, editProductResult, ({ args, result }) => {
  const sameItems = result.length === args.items.length;
  return (
    <Stack gap="sm">
      <ProductChanges args={args} result={sameItems ? result : undefined} />
      <Stack gap={4}>
        <Line>
          <strong>{editedProductsLabel(result)}</strong>
        </Line>
        {result.map((row, index) => {
          const item = sameItems ? args.items[index] : undefined;
          if (recordedRename(item, row)) return null;
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

const edits = definePreview(editProductArguments, ({ args, requestId }) => (
  <GrocyReview requestId={requestId}>
    <ProductChanges args={args} />
  </GrocyReview>
));

function ProductEditSummary({ args, requestId }: PreviewProps<z.infer<typeof editProductArguments>>): JSX.Element {
  const refs = useGrocyRequestReferences(requestId);
  return (
    <Stack gap={4}>
      {args.items.slice(0, 2).map((item, index) => (
        <Stack key={index} gap={2}>
          <Line>
            {typeof item.product === "number"
              ? refs?.get(`product_edits/${item.product}`)?.name || `Product #${item.product}`
              : item.product}
            {item.name != null && <> → {item.name}</>}
          </Line>
          {item.location != null && <Line>Default location · {displayRef(item.location, "locations")}</Line>}
          {item.clear_fields?.includes("description") ? (
            <Line>Clear description</Line>
          ) : (
            item.description != null && <Line>Replace description</Line>
          )}
        </Stack>
      ))}
      {args.items.length > 2 && <Line>{args.items.length - 2} more products</Line>}
    </Stack>
  );
}

const compactProductEdit = z.strictObject({
  items: z.tuple([
    z.strictObject({
      product: z.int().positive(),
      name: z.string().trim().min(1).max(120).nullish(),
      location: z.int().positive().nullish(),
      description: z.string().max(160).nullish(),
      clear_fields: z.tuple([z.literal("description")]).nullish(),
    }),
  ]),
});

function ProductEditDecision({ args, requestId, buttons, fallback }: CardDecisionProps): ReactNode {
  const parsed = compactProductEdit.safeParse(args);
  const refs = useGrocyRequestReferences(parsed.success ? requestId : undefined);
  if (!parsed.success) return fallback;
  const [item] = parsed.data.items;
  const target = refs?.get(`product_edits/${item.product}`);
  const current = target?.productEditCurrent;
  if (!target?.name.trim() || current === undefined) return fallback;
  if (item.location != null) {
    if (current.locationId == null || current.locationId <= 0n || !Number.isSafeInteger(Number(current.locationId)))
      return fallback;
    const previous = refs?.get(`locations/${current.locationId}`)?.name.trim();
    const next = refs?.get(`locations/${item.location}`)?.name.trim();
    if (!previous || !next || (previous === next && current.locationId !== BigInt(item.location))) return fallback;
  }
  const changesDescription = item.description != null || item.clear_fields != null;
  if (item.description != null && item.clear_fields != null) return fallback;
  return item.name != null || item.location != null || changesDescription ? buttons : fallback;
}

const stockGetArguments = z.strictObject({
  products: z.array(reference).optional(),
  locations: z.array(reference).optional(),
});
const scopedStock = definePreview(stockGetArguments, ({ args, requestId }) => {
  const filters = [
    args.products && args.products.length > 0 && (
      <>
        Products ·{" "}
        {args.products.map((value, i) => (
          <span key={i}>
            {i > 0 && ", "}
            {displayRef(value, "products")}
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
            {displayRef(value, "locations")}
          </span>
        ))}
      </>
    ),
  ].filter(Boolean);
  return (
    <GrocyReview requestId={requestId}>
      <Stack gap={4}>
        {filters.length === 0 ? (
          <Line>All products and locations</Line>
        ) : (
          filters.map((filter, index) => <Line key={index}>{filter}</Line>)
        )}
      </Stack>
    </GrocyReview>
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

const grocyRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ...grocyStockRenderers,
  ...grocyShoppingRenderers,
  ...grocyCatalogRenderers,
  ...grocyEntityRenderers,
  ["products_list", listPresentation("List Grocy products", listArguments, productsListResult)],
  ["quantity_units_list", listPresentation("List quantity units", listArguments, quantityUnitsListResult)],
  ["get_system_info", listPresentation("Show Grocy system information", noArguments, systemInfoResult)],
  [
    "products_edit",
    {
      fallbackLabel: "Edit Grocy products",
      call: editProductCall,
      pane: {
        collapsed: definePreview(editProductArguments, (props) => (
          <GrocyReview requestId={props.requestId}>
            <ProductEditSummary {...props} />
          </GrocyReview>
        )),
        opened: edits,
      },
      details: { arguments: edits, pendingPreviewOnly: true, result: editProductReceipt },
      liveCardDecision: { component: ProductEditDecision },
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
      details: { arguments: scopedStock, pendingPreviewOnly: true, result: stockGetResult },
      compactCardExpands: expandsForRejectedArguments(stockGetArguments),
    },
  ],
]);

export const grocyPresentationGroup: readonly [string, typeof grocyRenderers] = ["grocy_sf", grocyRenderers];
