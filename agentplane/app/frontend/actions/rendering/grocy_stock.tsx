import { Stack } from "@mantine/core";
import type { ReactNode } from "react";
import { z } from "zod";

import { definePreview } from "./entry";
import { batchError, batchOutcome, displayRef, grocyRequest, Line, objectLink, reference } from "./grocy_shared";
import type { ActionPresentation } from "./presentation";
import { defineResultPreview, type ResultPreview } from "./result_entry";

const id = z.number().int();
const apiId = z.union([id, z.string().regex(/^\d+$/).transform(Number)]);
const apiAmount = z.union([
  z.number(),
  z
    .string()
    .regex(/^-?\d+(?:\.\d+)?$/)
    .transform(Number),
]);
const date = z.string().nullish();
const noArguments = z.strictObject({});
const productArguments = z.strictObject({ productId: id });
const stockScope = { product: reference, qu: reference, location: reference };

function dateLabel(value: string): string {
  return value === "2999-12-31" ? "Never expires" : `Best before ${value}`;
}

function Fields({ values }: { values: [string, ReactNode][] }) {
  return values.map(([label, value]) =>
    value !== undefined && value !== null && value !== false ? (
      <Line key={label}>
        {label} · {value === "" ? "(empty text)" : value}
      </Line>
    ) : null
  );
}

function rows<S extends z.ZodType>(item: S) {
  return z.strictObject({ items: z.array(item) });
}

const addArgs = rows(
  z.strictObject({
    ...stockScope,
    amount: z.number(),
    best_before_date: date,
    price: z.number().nullish(),
    note: z.string().nullish(),
  })
);
const consumeArgs = rows(
  z.strictObject({
    ...stockScope,
    amount: z.number(),
    spoiled: z.boolean().optional(),
    allow_subproduct_substitution: z.boolean().optional(),
  })
);
const setArgs = rows(
  z.strictObject({
    ...stockScope,
    qu: reference.nullish(),
    new_amount: z.number(),
    best_before_date: date,
    price: z.number().nullish(),
  })
);
const moveArgs = z.strictObject({
  product: reference,
  amount: z.number(),
  qu: reference,
  from_location: reference,
  to_location: reference,
});
const entryArgs = z
  .strictObject({ products: z.array(reference).nullish(), entry_ids: z.array(id).nullish() })
  .refine((args) => (args.products == null) !== (args.entry_ids == null));
const editArgs = rows(
  z.strictObject({
    entry_id: id,
    amount: z.number().nullish(),
    best_before_date: date,
    purchased_date: date,
    price: z.number().nullish(),
    location: reference.nullish(),
    open: z.boolean().nullish(),
    note: z.string().nullish(),
    clear_fields: z.array(z.enum(["price", "purchased_date", "note"])).nullish(),
  })
);
const expirationArgs = z.strictObject({ days_ahead: id });
const volatileArgs = z.strictObject({ due_soon_days: id.optional() });
const openArgs = productArguments.extend({
  amount: z.number(),
  stock_entry_id: z.string().optional(),
  allow_subproduct_substitution: z.boolean().optional(),
});
const undoArgs = z.strictObject({ transactionId: z.string() });

const stockOp = z.strictObject({
  kind: z.literal("ok"),
  product_name: z.string(),
  transaction_id: z.string().nullish(),
  amount_delta: z.number().nullish(),
  new_amount: z.number().nullish(),
  qu_name: z.string(),
  stock_qu_name: z.string().nullish(),
  location_name: z.string(),
  entry_id: id.nullish(),
  best_before_date: date,
});
const stockOpRow = z.union([stockOp, batchError]);
const stockOps = z.array(stockOpRow);

function StockChanges({ entries, verb }: { entries: z.infer<typeof stockOps>; verb: string }) {
  const count = entries.filter((row) => row.kind === "ok").length;
  return (
    <Stack gap="xs">
      <Line>
        {count} {count === 1 ? "item" : "items"} {verb}
      </Line>
      {entries.map((row, index) => (
        <Stack gap={2} key={index}>
          {row.kind === "error" ? (
            <Line>Failed · {row.error}</Line>
          ) : (
            <>
              <Line>
                <strong>{row.product_name}</strong> · {row.location_name.replace(" -> ", " → ")}
              </Line>
              {row.amount_delta != null && (
                <Line>
                  Change · {row.amount_delta > 0 ? "+" : ""}
                  {row.amount_delta} {row.stock_qu_name ?? row.qu_name}
                </Line>
              )}
              {row.new_amount != null && (
                <Line>
                  Now {row.new_amount} {row.stock_qu_name ?? row.qu_name} total
                </Line>
              )}
              {row.best_before_date && <Line>{dateLabel(row.best_before_date)}</Line>}
              {row.entry_id != null && <Line>{objectLink("stockentry", row.entry_id, "Open stock entry")}</Line>}
              {row.transaction_id && (
                <details>
                  <summary>Transaction reference</summary>
                  <Line>{row.transaction_id}</Line>
                </details>
              )}
            </>
          )}
        </Stack>
      ))}
    </Stack>
  );
}

const stockResult = (verb: string): ResultPreview =>
  defineResultPreview(stockOps, ({ result }) => <StockChanges entries={result} verb={verb} />);
function stockOutcome(value: unknown) {
  const parsed = stockOps.safeParse(value);
  return parsed.success ? batchOutcome(parsed.data) : undefined;
}

function scopedAmount(item: {
  product: string | number;
  amount: number;
  qu?: string | number | null;
  location: string | number;
}) {
  return (
    <Line>
      <strong>{displayRef(item.product, "products")}</strong> · {item.amount}{" "}
      {item.qu == null ? "stock units" : displayRef(item.qu, "quantity_units")} in{" "}
      {displayRef(item.location, "locations")}
    </Line>
  );
}

function StockBatch<T>({ items, show }: { items: T[]; show: (item: T) => ReactNode }) {
  return (
    <Stack gap={4}>
      {items.slice(0, 2).map((item, i) => (
        <div key={i}>{show(item)}</div>
      ))}
      {items.length > 2 && <Line>{items.length - 2} more</Line>}
    </Stack>
  );
}

const entry = z.strictObject({
  entry_id: id,
  product_id: id,
  product_name: z.string(),
  amount: z.number(),
  qu_name: z.string(),
  location_name: z.string(),
  best_before_date: date,
  purchased_date: date,
  price: z.number().nullish(),
  open: z.boolean().optional(),
  note: z.string().nullish(),
});
const updatedField = z.strictObject({ old: z.unknown(), new: z.unknown() });
const entries = z.array(
  z.discriminatedUnion("kind", [
    z.strictObject({ kind: z.literal("ok"), entry, changes: z.record(z.string(), updatedField).nullish() }),
    z.strictObject({ kind: z.literal("error"), error: z.string(), entry_id: id.nullish() }),
  ])
);

const fieldLabels: Record<string, string> = {
  amount: "Amount",
  best_before_date: "Best before",
  purchased_date: "Purchased",
  price: "Price",
  location_id: "Location",
  open: "Opened",
  note: "Note",
};

function changedValue(value: unknown): ReactNode {
  return value == null ? "(none)" : typeof value === "string" ? value || "(empty text)" : JSON.stringify(value);
}

const entryResult = defineResultPreview(entries, ({ result }) => (
  <Stack gap="xs">
    {result.length === 0 && <Line>No stock entries matched</Line>}
    {result.map((row, index) =>
      row.kind === "error" ? (
        <Line key={index}>
          Could not read or update stock entry{row.entry_id != null ? ` #${row.entry_id}` : ""} · {row.error}
        </Line>
      ) : (
        <Stack gap={2} key={index}>
          <Line>
            {objectLink("stockentry", row.entry.entry_id, row.entry.product_name)} · {row.entry.amount}{" "}
            {row.entry.qu_name} in {row.entry.location_name}
            {row.entry.open && " · Opened"}
          </Line>
          <Fields
            values={[
              [
                "Best before",
                row.entry.best_before_date === "2999-12-31" ? "Never expires" : row.entry.best_before_date,
              ],
              ["Purchased", row.entry.purchased_date],
              ["Price per unit", row.entry.price],
              ["Note", row.entry.note],
            ]}
          />
          {Object.entries(row.changes ?? {}).map(([name, values]) => (
            <Line key={name}>
              {Object.hasOwn(fieldLabels, name) ? fieldLabels[name] : name.replaceAll("_", " ")} ·{" "}
              {changedValue(values.old)} → {changedValue(values.new)}
            </Line>
          ))}
        </Stack>
      )
    )}
  </Stack>
));

const productStock = defineResultPreview(
  z.object({
    product: z.object({ id: apiId, name: z.string() }),
    quantity_unit_stock: z.object({ name: z.string(), name_plural: z.string().nullish() }),
    stock_amount: apiAmount,
    stock_amount_opened: apiAmount.nullish(),
    location: z.object({ name: z.string() }).nullish(),
    default_location: z.object({ name: z.string() }).nullish(),
    next_due_date: date,
  }),
  ({ result }) => (
    <Stack gap={4}>
      <Line>
        {objectLink("product", result.product.id, result.product.name)} · {result.stock_amount}{" "}
        {result.stock_amount === 1
          ? result.quantity_unit_stock.name
          : (result.quantity_unit_stock.name_plural ?? result.quantity_unit_stock.name)}
      </Line>
      <Fields
        values={[
          ["Opened", result.stock_amount_opened],
          ["Location", (result.default_location ?? result.location)?.name],
          ["Next due", result.next_due_date === "2999-12-31" ? "Never expires" : result.next_due_date],
        ]}
      />
    </Stack>
  )
);

const volatilityFields = { product_id: id, product_name: z.string(), amount: z.number(), qu_name: z.string() };
const expirationFields = { ...volatilityFields, location_name: z.string(), best_before_date: date };
const expiring = z.array(z.strictObject({ ...expirationFields, days_until_expiry: id }));
const expired = z.array(z.strictObject({ ...expirationFields, days_overdue: id }));
const below = z.array(z.strictObject({ ...volatilityFields, min_amount: z.number(), deficit: z.number() }));

function stockLabel(row: { product_id: number; product_name: string; amount: number; qu_name: string }) {
  return (
    <>
      {objectLink("product", row.product_id, row.product_name || "Open product")} · {row.amount} {row.qu_name}
    </>
  );
}

const expiringResult = defineResultPreview(expiring, ({ result }) => (
  <Stack gap={4}>
    {result.length === 0 && <Line>No stock due within this window</Line>}
    {result.map((row, i) => (
      <Line key={i}>
        {stockLabel(row)} in {row.location_name} · {row.best_before_date} ·{" "}
        {row.days_until_expiry === 0 ? "Due today" : `Due in ${row.days_until_expiry} d`}
      </Line>
    ))}
  </Stack>
));
const expiredResult = defineResultPreview(expired, ({ result }) => (
  <Stack gap={4}>
    {result.length === 0 && <Line>No expired stock</Line>}
    {result.map((row, i) => (
      <Line key={i}>
        {stockLabel(row)} in {row.location_name} · {row.best_before_date} · {row.days_overdue} d overdue
      </Line>
    ))}
  </Stack>
));
const belowResult = defineResultPreview(below, ({ result }) => (
  <Stack gap={4}>
    {result.length === 0 && <Line>No stock below minimum</Line>}
    {result.map((row, i) => (
      <Line key={i}>
        {stockLabel(row)} · Minimum {row.min_amount} · {row.deficit} short
      </Line>
    ))}
  </Stack>
));

const rawVolatileRows = z.array(
  z.object({ product_id: apiId, amount: apiAmount, best_before_date: date, product: z.object({ name: z.string() }) })
);
const volatileResult = defineResultPreview(
  z.object({
    due_products: rawVolatileRows,
    overdue_products: rawVolatileRows,
    expired_products: rawVolatileRows,
    missing_products: z.array(
      z.object({
        id: apiId,
        name: z.string().optional(),
        amount_missing: apiAmount,
        product: z.object({ name: z.string() }).optional(),
      })
    ),
  }),
  ({ result }) => {
    const groups = [
      ["Due soon", result.due_products],
      ["Overdue", result.overdue_products],
      ["Expired", result.expired_products],
    ] as const;
    return (
      <Stack gap="xs">
        {groups.map(([label, rows]) => (
          <Stack key={label} gap={2}>
            <Line>
              <strong>{label}</strong> · {rows.length}
            </Line>
            {rows.map((row, i) => (
              <Line key={i}>
                {objectLink("product", row.product_id, row.product.name)} · {row.amount} stock units
                {row.best_before_date && ` · ${row.best_before_date}`}
              </Line>
            ))}
          </Stack>
        ))}
        <Stack gap={2}>
          <Line>
            <strong>Below minimum</strong> · {result.missing_products.length}
          </Line>
          {result.missing_products.map((item) => (
            <Line key={item.id}>
              {objectLink("product", item.id, item.product?.name ?? item.name ?? "Open product")} ·{" "}
              {item.amount_missing} stock units short
            </Line>
          ))}
        </Stack>
      </Stack>
    );
  }
);

const openedReceipt = defineResultPreview(
  z.array(
    z.object({
      product_id: apiId,
      amount: apiAmount,
      transaction_id: z.string().nullish(),
      transaction_type: z.literal("product-opened"),
    })
  ),
  ({ result }) => (
    <Stack gap={4}>
      {result.length === 0 && <Line>No opening entries returned</Line>}
      {result.map((row, i) => (
        <Stack gap={2} key={i}>
          <Line>
            Marked {row.amount} stock units as opened · {objectLink("product", row.product_id, "Open product")}
          </Line>
          {row.transaction_id && (
            <details>
              <summary>Transaction reference</summary>
              <Line>{row.transaction_id}</Line>
            </details>
          )}
        </Stack>
      ))}
    </Stack>
  )
);

const undoReceipt = defineResultPreview(z.null(), () => <Line>Transaction undone</Line>);
const undoReview = definePreview(undoArgs, ({ args }) => <Line>Transaction · {args.transactionId}</Line>);

const unknownStockOutcome = (value: unknown) => {
  const parsed = entries.safeParse(value);
  return parsed.success ? batchOutcome(parsed.data) : undefined;
};

export const grocyStockRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "stock_add",
    {
      ...grocyRequest(
        "Add Grocy stock",
        addArgs,
        ({ args }) => (
          <Stack gap="xs">
            {args.items.map((item, i) => (
              <Stack gap={2} key={i}>
                {scopedAmount(item)}
                <Fields
                  values={[
                    ["Due date", item.best_before_date && dateLabel(item.best_before_date)],
                    ["Price per unit", item.price],
                    ["Note", item.note],
                  ]}
                />
              </Stack>
            ))}
          </Stack>
        ),
        stockResult("added"),
        ({ args }) => <StockBatch items={args.items} show={scopedAmount} />
      ),
      outcome: stockOutcome,
    },
  ],
  [
    "stock_consume",
    {
      ...grocyRequest(
        "Consume Grocy stock",
        consumeArgs,
        ({ args }) => (
          <Stack gap="xs">
            {args.items.map((item, i) => (
              <Stack gap={2} key={i}>
                {scopedAmount(item)}
                {item.spoiled && <Line>Record as spoilage</Line>}
                {item.allow_subproduct_substitution && <Line>Use variant stock if needed</Line>}
              </Stack>
            ))}
          </Stack>
        ),
        stockResult("consumed"),
        ({ args }) => (
          <StockBatch
            items={args.items}
            show={(item) => (
              <>
                {scopedAmount(item)}
                {item.spoiled && <Line>Record as spoilage</Line>}
                {item.allow_subproduct_substitution && <Line>Use variant stock if needed</Line>}
              </>
            )}
          />
        )
      ),
      outcome: stockOutcome,
    },
  ],
  [
    "stock_set",
    {
      ...grocyRequest(
        "Set Grocy stock totals",
        setArgs,
        ({ args }) => (
          <Stack gap="xs">
            {args.items.map((item, i) => (
              <Stack gap={2} key={i}>
                <Line>
                  <strong>{displayRef(item.product, "products")}</strong> · Set total to {item.new_amount}{" "}
                  {item.qu == null ? "stock units" : displayRef(item.qu, "quantity_units")}
                </Line>
                <Line>Storage for added units · {displayRef(item.location, "locations")}</Line>
                <Fields
                  values={[
                    ["Due date for added units", item.best_before_date && dateLabel(item.best_before_date)],
                    ["Price per added unit", item.price],
                  ]}
                />
              </Stack>
            ))}
          </Stack>
        ),
        stockResult("adjusted"),
        ({ args }) => (
          <StockBatch
            items={args.items}
            show={(item) => (
              <Line>
                {displayRef(item.product, "products")} · Set total to {item.new_amount}{" "}
                {item.qu == null ? "stock units" : displayRef(item.qu, "quantity_units")} ·{" "}
                {displayRef(item.location, "locations")}
              </Line>
            )}
          />
        )
      ),
      outcome: stockOutcome,
    },
  ],
  [
    "stock_transfer",
    {
      ...grocyRequest(
        "Transfer Grocy stock",
        moveArgs,
        ({ args }) => (
          <Stack gap={4}>
            <Line>
              <strong>{displayRef(args.product, "products")}</strong> · {args.amount}{" "}
              {displayRef(args.qu, "quantity_units")}
            </Line>
            <Line>
              {displayRef(args.from_location, "locations")} → {displayRef(args.to_location, "locations")}
            </Line>
            <Line>Moving into or out of a freezer may change the due date.</Line>
          </Stack>
        ),
        defineResultPreview(stockOpRow, ({ result }) => <StockChanges entries={[result]} verb="transferred" />),
        ({ args }) => (
          <Stack gap={4}>
            <Line>
              {args.amount} {displayRef(args.qu, "quantity_units")} · {displayRef(args.product, "products")}
            </Line>
            <Line>
              {displayRef(args.from_location, "locations")} → {displayRef(args.to_location, "locations")}
            </Line>
          </Stack>
        )
      ),
      outcome: (value) => {
        const parsed = stockOpRow.safeParse(value);
        return parsed.success ? batchOutcome([parsed.data]) : undefined;
      },
    },
  ],
  [
    "stock_entries_list",
    {
      ...grocyRequest(
        "View Grocy stock entries",
        entryArgs,
        ({ args }) => (
          <Line>
            {args.products ? "All entries for" : "Specific entries"} ·{" "}
            {(args.products ?? args.entry_ids ?? []).map((item, index) => (
              <span key={index}>
                {index > 0 && ", "}
                {displayRef(item, args.products ? "products" : "stock_entries")}
              </span>
            ))}
          </Line>
        ),
        entryResult,
        ({ args }) => (
          <StockBatch
            items={args.products ?? args.entry_ids ?? []}
            show={(item) => <Line>{displayRef(item, args.products ? "products" : "stock_entries")}</Line>}
          />
        )
      ),
      outcome: unknownStockOutcome,
    },
  ],
  [
    "stock_entry_edit",
    {
      ...grocyRequest(
        "Edit Grocy stock entries",
        editArgs,
        ({ args }) => (
          <Stack gap="xs">
            {args.items.map((item, i) => (
              <Stack gap={2} key={i}>
                <Line>{displayRef(item.entry_id, "stock_entries")}</Line>
                <Fields
                  values={[
                    ["Amount in stock units", item.amount],
                    ["Due date", item.best_before_date && dateLabel(item.best_before_date)],
                    ["Purchased", !item.clear_fields?.includes("purchased_date") && item.purchased_date],
                    ["Price per unit", !item.clear_fields?.includes("price") && item.price],
                    ["Location", item.location != null && displayRef(item.location, "locations")],
                    ["Opened", item.open == null ? null : item.open ? "Yes" : "No"],
                    ["Note", !item.clear_fields?.includes("note") && item.note],
                    ...[...new Set(item.clear_fields)].map((name): [string, string] => [fieldLabels[name], "Clear"]),
                  ]}
                />
              </Stack>
            ))}
          </Stack>
        ),
        entryResult,
        ({ args }) => (
          <StockBatch
            items={args.items}
            show={(item) => (
              <Line>
                {displayRef(item.entry_id, "stock_entries")}
                {item.amount != null && ` · Set amount to ${item.amount}`}
              </Line>
            )}
          />
        )
      ),
      outcome: unknownStockOutcome,
    },
  ],
  [
    "get_product_stock",
    grocyRequest(
      "View stock for a Grocy product",
      productArguments,
      ({ args }) => <Line>{displayRef(args.productId, "products")}</Line>,
      productStock
    ),
  ],
  [
    "get_expiring_stock",
    grocyRequest(
      "View expiring Grocy stock",
      expirationArgs,
      ({ args }) => <Line>Within the next {args.days_ahead} days</Line>,
      expiringResult
    ),
  ],
  [
    "get_expired_stock",
    grocyRequest(
      "View expired Grocy stock",
      noArguments,
      () => <Line>Stock past its expiration date</Line>,
      expiredResult
    ),
  ],
  [
    "get_below_minimum_stock",
    grocyRequest(
      "View Grocy stock below minimum",
      noArguments,
      () => <Line>Compare quantities with each product’s minimum</Line>,
      belowResult
    ),
  ],
  [
    "list_volatile_stock",
    grocyRequest(
      "View Grocy stock needing attention",
      volatileArgs,
      ({ args }) => <Line>Due within {args.due_soon_days ?? 5} days, overdue, expired, or below minimum</Line>,
      volatileResult
    ),
  ],
  [
    "open_product_stock",
    grocyRequest(
      "Mark Grocy stock as opened",
      openArgs,
      ({ args }) => (
        <Stack gap={4}>
          <Line>
            {args.amount} stock units of {displayRef(args.productId, "products")}
          </Line>
          {args.stock_entry_id && <Line>Exact stock entry · {args.stock_entry_id}</Line>}
          {args.allow_subproduct_substitution && <Line>Use variant stock if needed</Line>}
        </Stack>
      ),
      openedReceipt,
      ({ args }) => (
        <Stack gap={4}>
          <Line>
            {args.amount} stock units of {displayRef(args.productId, "products")}
          </Line>
          {args.stock_entry_id && <Line>Exact stock entry · {args.stock_entry_id}</Line>}
          {args.allow_subproduct_substitution && <Line>Use variant stock if needed</Line>}
        </Stack>
      )
    ),
  ],
  [
    "transaction_undo",
    {
      fallbackLabel: "Undo Grocy stock transaction",
      pane: { collapsed: undoReview, opened: undoReview },
      details: { arguments: undoReview, result: undoReceipt, emptyResult: <Line>Transaction undone</Line> },
    },
  ],
]);
