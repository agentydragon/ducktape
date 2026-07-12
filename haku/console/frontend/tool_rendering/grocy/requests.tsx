// Per-tool-type rendering for the remote `grocy-sf` MCP server (see
// grocy_mcp/README.md and grocy_mcp/batch_tools.py). Falls back to the generic raw-JSON
// view for anything that isn't shaped as expected — same caveat as kubectl/requests.tsx:
// arguments are only validated by the tool's own schema at execution time, not at submission.
//
// grocy-sf's tool surface is generated from Grocy's own OpenAPI spec plus custom batch
// tools (grocy_mcp/batch_tools.py). Because it is a remote operator-OAuth server rather than an
// in-process console server, its tools/list schemas are not available to the build-time catalog;
// these are hand-authored here against grocy_mcp/mcp_types.py's `AddItem` / `ConsumeItem` /
// `CreateProductItem`. Every tool call runs as the approving operator's own linked Grocy
// account (operator_oauth) once approved.
//
// `product` / `location` / `qu` / `product_group` / `parent_product` / `shopping_list`
// arguments accept either a name or a numeric ID (grocy_mcp's `EntityResolver` resolves either at
// execution time); an ID alone renders poorly, so `useGrocyReference` fetches `{id, name}` lookups
// once per widget via GET /api/grocy-sf/reference and every row resolves through it.
//
// `products_edit` renders an old→new diff, so the reference carries each product's *current*
// field values (its `products` entries are full records, not just `{id, name}`); the widget looks
// the edited product up by name/ID and resolves its old foreign keys through the same maps. While
// the reference is still loading the old side is simply omitted (only the new value shows).

import { Badge, Group, Stack, Text } from "@mantine/core";
import { Fragment, type ReactNode, useEffect, useState } from "react";
import type { z } from "zod";

import { fetchGrocyReference, type GrocyReferenceResponse } from "../../grocy_client.ts";
import { mcpToolSchema } from "../../mcp_tool_schema.ts";
import { definePreview, type ToolPreview } from "../entry.tsx";
import { COMPACT_ITEM_LIMIT, MoreLine, plural, type PreviewProps, type PreviewVariant } from "../variant.tsx";

export const GROCY_SERVER_ID = "grocy-sf";

// Argument validators generated from grocy_mcp's own Pydantic models (`grocy_mcp/mcp_types.py`):
// the batch tools are reflected at build time (haku/console/export_mcp_tool_schemas.py) so these
// stay in lockstep with the server instead of being hand-copied. `int | str` name-or-ID fields
// come through as `string | number`; a `date` field as `string`; a `set` as an array.
const zStockAddArgs = mcpToolSchema(GROCY_SERVER_ID, "stock_add");
const zStockConsumeArgs = mcpToolSchema(GROCY_SERVER_ID, "stock_consume");
const zStockEntryEditArgs = mcpToolSchema(GROCY_SERVER_ID, "stock_entry_edit");
const zProductsCreateArgs = mcpToolSchema(GROCY_SERVER_ID, "products_create");
const zProductsEditArgs = mcpToolSchema(GROCY_SERVER_ID, "products_edit");
const zShoppingListGetArgs = mcpToolSchema(GROCY_SERVER_ID, "shopping_list_get");
const zShoppingListItemsAddArgs = mcpToolSchema(GROCY_SERVER_ID, "shopping_list_items_add");
const zShoppingListItemEditArgs = mcpToolSchema(GROCY_SERVER_ID, "shopping_list_item_edit");

type StockAddArgs = z.infer<typeof zStockAddArgs>;
type StockConsumeArgs = z.infer<typeof zStockConsumeArgs>;
type StockEntryEditArgs = z.infer<typeof zStockEntryEditArgs>;
type ProductsCreateArgs = z.infer<typeof zProductsCreateArgs>;
type ProductsEditArgs = z.infer<typeof zProductsEditArgs>;
type ShoppingListGetArgs = z.infer<typeof zShoppingListGetArgs>;
type ShoppingListItemsAddArgs = z.infer<typeof zShoppingListItemsAddArgs>;
type ShoppingListItemEditArgs = z.infer<typeof zShoppingListItemEditArgs>;

type AddItem = StockAddArgs["items"][number];
type ConsumeItem = StockConsumeArgs["items"][number];
type StockEntryEditItem = StockEntryEditArgs["items"][number];
type CreateProductItem = ProductsCreateArgs["items"][number];
type EditProductItem = ProductsEditArgs["items"][number];
type ShoppingItem = ShoppingListItemsAddArgs["items"][number];
// The clearable-field names, from the generated `clear_fields` element type.
type EditProductField = NonNullable<EditProductItem["clear_fields"]>[number];

// One product's current field values, as the reference carries them (see grocy_client.ts).
type GrocyProduct = GrocyReferenceResponse["products"][number];

// Fetched once per rendered preview; while loading (or on fetch failure) `resolveName`
// falls back to `id=N` for numeric references — a name argument always renders as-is.
function useGrocyReference(): { reference: GrocyReferenceResponse | null; error: string | null } {
  const [reference, setReference] = useState<GrocyReferenceResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchGrocyReference()
      .then((result) => {
        if (alive) setReference(result);
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  return { reference, error };
}

function resolveName(items: { id: number; name: string }[] | undefined, value: string | number): string {
  if (typeof value === "string") return value;
  return items?.find((item) => item.id === value)?.name ?? `id=${value}`;
}

// Find the full current record for the edited product — a string arg is a name, a number an ID
// (grocy_mcp accepts either). Undefined while the reference loads or if the name doesn't match.
function resolveProduct(products: GrocyProduct[] | undefined, value: string | number): GrocyProduct | undefined {
  return typeof value === "number" ? products?.find((p) => p.id === value) : products?.find((p) => p.name === value);
}

function GrocyReferenceLoadError({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <Text size="sm" c="red">
      Couldn't resolve product/location names: {error}
    </Text>
  );
}

// Shared skeleton for the item-list previews (stock add/consume, products create/edit, shopping
// add): fetch the reference once, render the first few rows compact / all detailed with a
// "… +N more" line, and surface a reference load error. `gap` sets the inter-row spacing (larger
// for multi-line rows). `renderRow` gets the reference and variant so a row can resolve names and
// pick its own compact/detailed form.
function GrocyItemsPreview<T>({
  items,
  variant,
  gap,
  renderRow,
}: {
  items: T[];
  variant: PreviewVariant;
  gap: number;
  renderRow: (item: T, reference: GrocyReferenceResponse | null, variant: PreviewVariant) => ReactNode;
}) {
  const { reference, error } = useGrocyReference();
  const shown = variant === "compact" ? items.slice(0, COMPACT_ITEM_LIMIT) : items;
  return (
    <Stack gap="xs">
      <Stack gap={gap}>
        {shown.map((item, i) => (
          <Fragment key={i}>{renderRow(item, reference, variant)}</Fragment>
        ))}
        <MoreLine count={items.length - shown.length} />
      </Stack>
      <GrocyReferenceLoadError error={error} />
    </Stack>
  );
}

function ProductAmount({ product, amount, qu }: { product: string; amount: number; qu: string }) {
  return (
    <Group gap={4}>
      <Text span fw={600}>
        {product}
      </Text>
      <Text span c="dimmed">
        ×
      </Text>
      <Text span>
        {amount} {qu}
      </Text>
    </Group>
  );
}

function StockAddRow({ item, reference }: { item: AddItem; reference: GrocyReferenceResponse | null }) {
  return (
    <Group gap={6}>
      <ProductAmount
        product={resolveName(reference?.products, item.product)}
        amount={item.amount}
        qu={resolveName(reference?.quantity_units, item.qu)}
      />
      <Text span c="dimmed">
        → {resolveName(reference?.locations, item.location)}
      </Text>
      {item.best_before_date && (
        <Badge size="sm" variant="outline">
          best before {item.best_before_date}
        </Badge>
      )}
    </Group>
  );
}

function StockAddPreview({ args, variant }: PreviewProps<StockAddArgs>) {
  return (
    <GrocyItemsPreview
      items={args.items}
      variant={variant}
      gap={4}
      renderRow={(item, reference) => <StockAddRow item={item} reference={reference} />}
    />
  );
}

function StockConsumeRow({ item, reference }: { item: ConsumeItem; reference: GrocyReferenceResponse | null }) {
  return (
    <Group gap={6}>
      <ProductAmount
        product={resolveName(reference?.products, item.product)}
        amount={item.amount}
        qu={resolveName(reference?.quantity_units, item.qu)}
      />
      <Text span c="dimmed">
        from {resolveName(reference?.locations, item.location)}
      </Text>
      {item.spoiled && (
        <Badge size="sm" color="orange" variant="outline">
          spoiled
        </Badge>
      )}
    </Group>
  );
}

function StockConsumePreview({ args, variant }: PreviewProps<StockConsumeArgs>) {
  return (
    <GrocyItemsPreview
      items={args.items}
      variant={variant}
      gap={4}
      renderRow={(item, reference) => <StockConsumeRow item={item} reference={reference} />}
    />
  );
}

// -1 = never expires, 0 = same-day, N>0 = N days — mirrors DEFAULT_BBD_DESC in
// grocy_mcp/mcp_types.py.
function formatDefaultBestBeforeDays(days: number): string {
  if (days === -1) return "never expires";
  if (days === 0) return "same-day";
  return `${days} day${days === 1 ? "" : "s"}`;
}

// due_type: 1 = best-before (soft), 2 = expiration (hard) — mirrors DUE_TYPE_DESC.
function formatDueType(dueType: number): string {
  return dueType === 1 ? "best before" : "due date";
}

function ProductsCreateRow({
  item,
  reference,
  variant,
}: {
  item: CreateProductItem;
  reference: GrocyReferenceResponse | null;
  variant: PreviewVariant;
}) {
  const stockQuName = resolveName(reference?.quantity_units, item.stock_qu);
  const purchaseQuName = item.purchase_qu != null ? resolveName(reference?.quantity_units, item.purchase_qu) : null;
  const consumeQuName = item.consume_qu != null ? resolveName(reference?.quantity_units, item.consume_qu) : null;
  return (
    <Stack gap={2}>
      <Group gap={6}>
        <Text span fw={600}>
          {item.name}
        </Text>
        <Text span c="dimmed">
          stock in {stockQuName}, at {resolveName(reference?.locations, item.location)}
        </Text>
        {item.product_group != null && (
          <Badge size="sm">{resolveName(reference?.product_groups, item.product_group)}</Badge>
        )}
      </Group>
      {/* Compact rows carry just the product's name + where it stocks; the secondary badge
          row (shelf life, min stock, alternate units, parent) and description are detail-only. */}
      {variant === "compact" ? null : (
        <>
          <Group gap={6}>
            <Badge size="sm" variant="outline" color="gray">
              shelf life: {formatDefaultBestBeforeDays(item.default_best_before_days)}
            </Badge>
            {item.min_stock_amount != null && item.min_stock_amount !== 0 && (
              <Badge size="sm" variant="outline" color="gray">
                min stock: {item.min_stock_amount} {stockQuName}
              </Badge>
            )}
            {purchaseQuName && purchaseQuName !== stockQuName && (
              <Badge size="sm" variant="outline" color="gray">
                purchased in {purchaseQuName}
              </Badge>
            )}
            {consumeQuName && consumeQuName !== stockQuName && (
              <Badge size="sm" variant="outline" color="gray">
                consumed in {consumeQuName}
              </Badge>
            )}
            {item.parent_product != null && (
              <Badge size="sm" variant="outline" color="gray">
                variant of {resolveName(reference?.products, item.parent_product)}
              </Badge>
            )}
          </Group>
          {item.description && (
            <Text size="sm" c="dimmed">
              {item.description}
            </Text>
          )}
        </>
      )}
    </Stack>
  );
}

function ProductsCreatePreview({ args, variant }: PreviewProps<ProductsCreateArgs>) {
  return (
    <GrocyItemsPreview
      items={args.items}
      variant={variant}
      gap={6}
      renderRow={(item, reference, v) => <ProductsCreateRow item={item} reference={reference} variant={v} />}
    />
  );
}

// One `field: old → new` row. `old === null` means the current value isn't known yet (reference
// still loading) — only the new value shows. `CLEARED` marks a `clear_fields` removal.
const CLEARED = "(cleared)";
const NONE = "(none)";

type FieldChange = { key: string; label: string; old: string | null; next: string };

function ChangeLine({ change }: { change: FieldChange }) {
  return (
    <Text size="sm">
      <Text span c="dimmed">
        {change.label}:{" "}
      </Text>
      {change.old !== null && (
        <Text span c="dimmed">
          {change.old || "(empty)"} →{" "}
        </Text>
      )}
      <Text span>{change.next}</Text>
    </Text>
  );
}

function StockEntryEditRow({
  item,
  reference,
  variant,
}: {
  item: StockEntryEditItem;
  reference: GrocyReferenceResponse | null;
  variant: PreviewVariant;
}) {
  const changes: FieldChange[] = [];
  const add = (key: string, label: string, value: unknown) => {
    if (value != null) changes.push({ key, label, old: null, next: String(value) });
  };
  add("amount", "amount", item.amount);
  add("best_before_date", "best before", item.best_before_date);
  add("purchased_date", "purchased", item.purchased_date);
  add("price", "price", item.price);
  if (item.location != null) {
    changes.push({
      key: "location",
      label: "location",
      old: null,
      next: resolveName(reference?.locations, item.location),
    });
  }
  if (item.open != null) add("open", "opened", item.open ? "yes" : "no");
  add("note", "note", item.note);
  for (const field of item.clear_fields ?? []) {
    changes.push({ key: `clear_${field}`, label: field.replaceAll("_", " "), old: null, next: CLEARED });
  }
  const shown = variant === "compact" ? changes.slice(0, 2) : changes;
  return (
    <Stack gap={2}>
      <Text fw={600}>Stock entry #{item.entry_id}</Text>
      {shown.map((change) => (
        <ChangeLine key={change.key} change={change} />
      ))}
      <MoreLine count={changes.length - shown.length} />
    </Stack>
  );
}

function StockEntryEditPreview({ args, variant }: PreviewProps<StockEntryEditArgs>) {
  return (
    <GrocyItemsPreview
      items={args.items}
      variant={variant}
      gap={6}
      renderRow={(item, reference, v) => <StockEntryEditRow item={item} reference={reference} variant={v} />}
    />
  );
}

type NameMap = { id: number; name: string }[] | undefined;

// The old value to show for a field: `null` while the reference loads (so no old side renders),
// else the resolved value or "(none)" when the field is currently unset.
function oldValue(current: GrocyProduct | undefined, resolved: string | null): string | null {
  return current ? (resolved ?? NONE) : null;
}

// The product's current value for a foreign-key column, resolved to a name through `map`.
function currentRef(current: GrocyProduct | undefined, oldId: number | null | undefined, map: NameMap): string | null {
  return oldValue(current, oldId != null ? resolveName(map, oldId) : null);
}

// One foreign-key field's change: the new arg and the product's current id resolve through the
// same map. `null` when the edit doesn't touch this field.
function refChange(
  key: string,
  label: string,
  newRef: string | number | null | undefined,
  oldId: number | null | undefined,
  map: NameMap,
  current: GrocyProduct | undefined
): FieldChange | null {
  return newRef == null ? null : { key, label, old: currentRef(current, oldId, map), next: resolveName(map, newRef) };
}

function clearChange(
  field: EditProductField,
  current: GrocyProduct | undefined,
  reference: GrocyReferenceResponse | null
): FieldChange {
  const base = { key: `clear_${field}`, next: CLEARED };
  switch (field) {
    case "description":
      return { ...base, label: "description", old: oldValue(current, current?.description ?? null) };
    case "product_group":
      return {
        ...base,
        label: "group",
        old: currentRef(current, current?.product_group_id, reference?.product_groups),
      };
    case "parent_product":
      return {
        ...base,
        label: "variant of",
        old: currentRef(current, current?.parent_product_id, reference?.products),
      };
    case "calories":
      return {
        ...base,
        label: "calories",
        old: oldValue(current, current?.calories != null ? String(current.calories) : null),
      };
  }
}

function productEditChanges(
  item: EditProductItem,
  current: GrocyProduct | undefined,
  reference: GrocyReferenceResponse | null,
  variant: PreviewVariant
): FieldChange[] {
  const qu = reference?.quantity_units;
  const oldStockUnit = current ? resolveName(qu, current.qu_id_stock) : null;
  const newStockUnit = item.stock_qu != null ? resolveName(qu, item.stock_qu) : oldStockUnit;
  const minStock = (amount: number, unit: string | null) => `${amount} ${unit ?? "stock units"}`;

  const changes: (FieldChange | null)[] = [
    item.name != null ? { key: "name", label: "name", old: current?.name ?? null, next: item.name } : null,
    refChange("stock_qu", "stock unit", item.stock_qu, current?.qu_id_stock, qu, current),
    refChange("location", "location", item.location, current?.location_id, reference?.locations, current),
    refChange("purchase_qu", "purchase unit", item.purchase_qu, current?.qu_id_purchase, qu, current),
    refChange("consume_qu", "consume unit", item.consume_qu, current?.qu_id_consume, qu, current),
    item.min_stock_amount != null
      ? {
          key: "min_stock",
          label: "min stock",
          old: current ? minStock(current.min_stock_amount, oldStockUnit) : null,
          next: minStock(item.min_stock_amount, newStockUnit),
        }
      : null,
    item.default_best_before_days != null
      ? {
          key: "bbd",
          label: "shelf life",
          old: current ? formatDefaultBestBeforeDays(current.default_best_before_days) : null,
          next: formatDefaultBestBeforeDays(item.default_best_before_days),
        }
      : null,
    item.due_type != null
      ? {
          key: "due_type",
          label: "due type",
          old: current ? formatDueType(current.due_type) : null,
          next: formatDueType(item.due_type),
        }
      : null,
    refChange("parent", "variant of", item.parent_product, current?.parent_product_id, reference?.products, current),
    refChange("group", "group", item.product_group, current?.product_group_id, reference?.product_groups, current),
    // A description can be long; compact just flags it changed, detailed shows the full old→new.
    item.description != null
      ? variant === "compact"
        ? { key: "description", label: "description", old: null, next: "updated" }
        : {
            key: "description",
            label: "description",
            old: oldValue(current, current?.description ?? null),
            next: item.description,
          }
      : null,
  ];

  for (const field of item.clear_fields ?? []) changes.push(clearChange(field, current, reference));
  return changes.filter((c): c is FieldChange => c != null);
}

function ProductsEditRow({
  item,
  reference,
  variant,
}: {
  item: EditProductItem;
  reference: GrocyReferenceResponse | null;
  variant: PreviewVariant;
}) {
  const current = resolveProduct(reference?.products, item.product);
  const changes = productEditChanges(item, current, reference, variant);
  const shown = variant === "compact" ? changes.slice(0, 2) : changes;
  return (
    <Stack gap={2}>
      <Text fw={600}>{resolveName(reference?.products, item.product)}</Text>
      <Stack gap={2}>
        {shown.map((change) => (
          <ChangeLine key={change.key} change={change} />
        ))}
        <MoreLine count={changes.length - shown.length} />
      </Stack>
    </Stack>
  );
}

function ProductsEditPreview({ args, variant }: PreviewProps<ProductsEditArgs>) {
  return (
    <GrocyItemsPreview
      items={args.items}
      variant={variant}
      gap={6}
      renderRow={(item, reference, v) => <ProductsEditRow item={item} reference={reference} variant={v} />}
    />
  );
}

// ── Shopping-list tools ──────────────────────────────────────────────────────

function ShoppingListGetPreview({ args }: PreviewProps<ShoppingListGetArgs>) {
  const { reference, error } = useGrocyReference();
  return (
    <Stack gap="xs">
      <Text>
        <Text span c="dimmed">
          List{" "}
        </Text>
        <Text span fw={600}>
          {resolveName(reference?.shopping_lists, args.shopping_list)}
        </Text>
      </Text>
      <GrocyReferenceLoadError error={error} />
    </Stack>
  );
}

function ShoppingListAddRow({ item, reference }: { item: ShoppingItem; reference: GrocyReferenceResponse | null }) {
  return (
    <Group gap={6}>
      {item.product != null ? (
        <Group gap={4}>
          <Text span fw={600}>
            {resolveName(reference?.products, item.product)}
          </Text>
          {item.amount != null && item.amount !== 1 && (
            <Text span c="dimmed">
              × {item.amount}
            </Text>
          )}
        </Group>
      ) : (
        <Text span fw={600}>
          {item.note}
        </Text>
      )}
      <Text span c="dimmed">
        → {resolveName(reference?.shopping_lists, item.shopping_list)}
      </Text>
      {item.product != null && item.note && (
        <Text span size="sm" c="dimmed">
          ({item.note})
        </Text>
      )}
    </Group>
  );
}

function ShoppingListItemsAddPreview({ args, variant }: PreviewProps<ShoppingListItemsAddArgs>) {
  return (
    <GrocyItemsPreview
      items={args.items}
      variant={variant}
      gap={4}
      renderRow={(item, reference) => <ShoppingListAddRow item={item} reference={reference} />}
    />
  );
}

// Shopping-list items are keyed only by `item_id`, and the reference holds no item detail, so
// (unlike products_edit) the edit preview shows the new values without an old→new diff.
function shoppingItemEditChanges(args: ShoppingListItemEditArgs): FieldChange[] {
  return [
    args.amount != null ? { key: "amount", label: "amount", old: null, next: String(args.amount) } : null,
    args.note != null ? { key: "note", label: "note", old: null, next: args.note } : null,
    args.done != null ? { key: "done", label: "done", old: null, next: args.done ? "yes" : "no" } : null,
    ...(args.clear_fields ?? []).map(
      (field): FieldChange => ({ key: `clear_${field}`, label: field, old: null, next: CLEARED })
    ),
  ].filter((c): c is FieldChange => c != null);
}

function ShoppingListItemEditPreview({ args, variant }: PreviewProps<ShoppingListItemEditArgs>) {
  const changes = shoppingItemEditChanges(args);
  const shown = variant === "compact" ? changes.slice(0, 2) : changes;
  return (
    <Stack gap={2}>
      <Text fw={600}>Item #{args.item_id}</Text>
      <Stack gap={2}>
        {shown.map((change) => (
          <ChangeLine key={change.key} change={change} />
        ))}
        <MoreLine count={changes.length - shown.length} />
      </Stack>
    </Stack>
  );
}

/** Per-tool preview widgets for the `grocy-sf` server's stock, product, and shopping-list tools. */
export const grocyPreviews = {
  stock_add: definePreview(zStockAddArgs, StockAddPreview, (a) => ({
    text: `Grocy: Add ${plural(a.items.length, "item")} to stock`,
  })),
  stock_consume: definePreview(zStockConsumeArgs, StockConsumePreview, (a) => ({
    text: `Grocy: Remove ${plural(a.items.length, "item")} from stock`,
  })),
  stock_entry_edit: definePreview(zStockEntryEditArgs, StockEntryEditPreview, (a) => ({
    text: `Grocy: Edit ${a.items.length} stock ${a.items.length === 1 ? "entry" : "entries"}`,
  })),
  products_create: definePreview(zProductsCreateArgs, ProductsCreatePreview, (a) => ({
    text: `Grocy: Create ${plural(a.items.length, "product")}`,
  })),
  products_edit: definePreview(zProductsEditArgs, ProductsEditPreview, (a) => ({
    text: `Grocy: Edit ${plural(a.items.length, "product")}`,
  })),
  shopping_list_get: definePreview(zShoppingListGetArgs, ShoppingListGetPreview, () => ({
    text: "Grocy: View shopping list",
  })),
  shopping_list_items_add: definePreview(zShoppingListItemsAddArgs, ShoppingListItemsAddPreview, (a) => ({
    text: `Grocy: Add ${plural(a.items.length, "item")} to shopping list`,
  })),
  shopping_list_item_edit: definePreview(zShoppingListItemEditArgs, ShoppingListItemEditPreview, () => ({
    text: "Grocy: Edit shopping list item",
  })),
} satisfies Record<string, ToolPreview>;
