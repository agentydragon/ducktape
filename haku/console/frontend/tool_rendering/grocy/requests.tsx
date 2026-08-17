// Per-tool-type rendering for the remote `grocy-sf` MCP server (see grocy_mcp/README.md and
// grocy_mcp/batch_tools.py). Anything not shaped as expected falls back to the generic raw-JSON
// view: as with kubectl/requests.tsx, arguments are validated by the tool's own schema at execution
// time, not at submission. Every approved call runs as the operator's own linked Grocy account
// (operator_oauth).
//
// `product` / `location` / `qu` / `product_group` / `parent_product` / `shopping_list` arguments
// accept either a name or a numeric ID (grocy_mcp's `GrocyClient` resolves either at execution
// time); an ID alone renders poorly, so `useGrocyReferenceData` composes the server's MCP read tools
// once per page and every row resolves through their results.
//
// `products_edit` renders an old→new diff, so the reference carries each product's *current* field
// values (its `products` entries are full records, not just `{id, name}`); the widget looks the
// edited product up by name/ID and resolves its old foreign keys through the same maps. While the
// reference is still loading the old side is omitted and only the new value shows.

import { Group, Stack } from "@mantine/core";
import { Fragment, type ReactNode, useEffect, useState } from "react";
import { z } from "zod";

import { Field } from "../../field";
import { fetchGrocyReferenceData, type GrocyReferenceData } from "../../grocy_client";
import { mcpToolSchema } from "../../mcp_tool_schema";
import { definePreview, type ToolPreview } from "../entry";
import { GROCY_SERVER_ID } from "../server_ids";
import {
  COMPACT_ITEM_LIMIT,
  MoreLine,
  PreviewBadge,
  PreviewText,
  PreviewTitle,
  type PreviewProps,
  type PreviewVariant,
} from "../vocabulary";

// Argument validators generated from grocy_mcp's own Pydantic models (`grocy_mcp/mcp_types.py`):
// the batch tools are reflected at build time (haku/console/export_mcp_tool_schemas.py) so these
// stay in lockstep with the server instead of being hand-copied. `int | str` name-or-ID fields
// come through as `string | number`; a `date` field as `string`; a `set` as an array.
const zStockAddArgs = mcpToolSchema(GROCY_SERVER_ID, "stock_add");
const zStockConsumeArgs = mcpToolSchema(GROCY_SERVER_ID, "stock_consume");
const zStockEntryEditArgs = mcpToolSchema(GROCY_SERVER_ID, "stock_entry_edit");
const zStockGetArgs = mcpToolSchema(GROCY_SERVER_ID, "stock_get");
const zProductsListArgs = mcpToolSchema(GROCY_SERVER_ID, "products_list");
const zQuantityUnitsListArgs = mcpToolSchema(GROCY_SERVER_ID, "quantity_units_list");
const zGetSystemInfoArgs = z.strictObject({});
const zProductsCreateArgs = mcpToolSchema(GROCY_SERVER_ID, "products_create");
const zProductsEditArgs = mcpToolSchema(GROCY_SERVER_ID, "products_edit");
const zShoppingListGetArgs = mcpToolSchema(GROCY_SERVER_ID, "shopping_list_get");
const zShoppingListItemsAddArgs = mcpToolSchema(GROCY_SERVER_ID, "shopping_list_items_add");
const zShoppingListItemsRemoveArgs = mcpToolSchema(GROCY_SERVER_ID, "shopping_list_items_remove");
const zShoppingListItemEditArgs = mcpToolSchema(GROCY_SERVER_ID, "shopping_list_item_edit");

type StockAddArgs = z.infer<typeof zStockAddArgs>;
type StockConsumeArgs = z.infer<typeof zStockConsumeArgs>;
type StockEntryEditArgs = z.infer<typeof zStockEntryEditArgs>;
type StockGetArgs = z.infer<typeof zStockGetArgs>;
type ProductsListArgs = z.infer<typeof zProductsListArgs>;
type QuantityUnitsListArgs = z.infer<typeof zQuantityUnitsListArgs>;
type GetSystemInfoArgs = z.infer<typeof zGetSystemInfoArgs>;
type ProductsCreateArgs = z.infer<typeof zProductsCreateArgs>;
type ProductsEditArgs = z.infer<typeof zProductsEditArgs>;
type ShoppingListGetArgs = z.infer<typeof zShoppingListGetArgs>;
type ShoppingListItemsAddArgs = z.infer<typeof zShoppingListItemsAddArgs>;
type ShoppingListItemsRemoveArgs = z.infer<typeof zShoppingListItemsRemoveArgs>;
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
type GrocyProduct = GrocyReferenceData["products"][number];

// Fetched once per rendered preview; while loading (or on fetch failure) `resolveName`
// falls back to `id=N` for numeric references — a name argument always renders as-is.
function useGrocyReferenceData(): { reference: GrocyReferenceData | null; error: string | null } {
  const [reference, setReference] = useState<GrocyReferenceData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchGrocyReferenceData()
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

// A shopping-list item's current record, carried by the reference so `shopping_list_item_edit`
// (and `shopping_list_items_remove`) can resolve a bare `item_id` to a product/note and show the
// item's current amount/note/done for an old→new diff.
type ShoppingListRefItem = GrocyReferenceData["shopping_list_items"][number];

function resolveShoppingItem(
  items: ShoppingListRefItem[] | undefined,
  itemId: number
): ShoppingListRefItem | undefined {
  return items?.find((item) => item.item_id === itemId);
}

// Identity label for a shopping-list item: product name, else its note, else a bare `Item #id`
// fallback (used while the reference loads or for an unknown id). Mirrors `shoppingItemName`.
function shoppingItemLabel(item: ShoppingListRefItem | undefined, itemId: number): string {
  return item?.product_name ?? item?.note ?? `Item #${itemId}`;
}

// `amount` rendered with its quantity unit, e.g. `3 pack` (no unit for note-only items).
function formatAmount(amount: number, qu: string | null | undefined): string {
  return `${amount}${qu ? ` ${qu}` : ""}`;
}

function GrocyReferenceLoadError({ error }: { error: string | null }) {
  if (!error) return null;
  return <PreviewText c="red">Couldn&apos;t resolve product/location names: {error}</PreviewText>;
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
  renderRow: (item: T, reference: GrocyReferenceData | null, variant: PreviewVariant) => ReactNode;
}) {
  const { reference, error } = useGrocyReferenceData();
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
      <PreviewText span fw={600}>
        {product}
      </PreviewText>
      <PreviewText span c="dimmed">
        ×
      </PreviewText>
      <PreviewText span>
        {amount} {qu}
      </PreviewText>
    </Group>
  );
}

function StockAddRow({ item, reference }: { item: AddItem; reference: GrocyReferenceData | null }) {
  return (
    <Group gap={6}>
      <ProductAmount
        product={resolveName(reference?.products, item.product)}
        amount={item.amount}
        qu={resolveName(reference?.quantity_units, item.qu)}
      />
      <PreviewText span c="dimmed">
        → {resolveName(reference?.locations, item.location)}
      </PreviewText>
      {item.best_before_date && <PreviewBadge variant="outline">best before {item.best_before_date}</PreviewBadge>}
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

function StockConsumeRow({ item, reference }: { item: ConsumeItem; reference: GrocyReferenceData | null }) {
  return (
    <Group gap={6}>
      <ProductAmount
        product={resolveName(reference?.products, item.product)}
        amount={item.amount}
        qu={resolveName(reference?.quantity_units, item.qu)}
      />
      <PreviewText span c="dimmed">
        from {resolveName(reference?.locations, item.location)}
      </PreviewText>
      {item.spoiled && (
        <PreviewBadge color="orange" variant="outline">
          spoiled
        </PreviewBadge>
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
  reference: GrocyReferenceData | null;
  variant: PreviewVariant;
}) {
  const stockQuName = resolveName(reference?.quantity_units, item.stock_qu);
  const purchaseQuName = item.purchase_qu != null ? resolveName(reference?.quantity_units, item.purchase_qu) : null;
  const consumeQuName = item.consume_qu != null ? resolveName(reference?.quantity_units, item.consume_qu) : null;
  return (
    <Stack gap={2}>
      <Group gap={6}>
        <PreviewText span fw={600}>
          {item.name}
        </PreviewText>
        <PreviewText span c="dimmed">
          stock in {stockQuName}, at {resolveName(reference?.locations, item.location)}
        </PreviewText>
        {item.product_group != null && (
          <PreviewBadge>{resolveName(reference?.product_groups, item.product_group)}</PreviewBadge>
        )}
      </Group>
      {/* Compact rows carry just the product's name + where it stocks; the secondary badge
          row (shelf life, min stock, alternate units, parent) and description are detail-only. */}
      {variant === "compact" ? null : (
        <>
          <Group gap={6}>
            <PreviewBadge variant="outline" color="gray">
              shelf life: {formatDefaultBestBeforeDays(item.default_best_before_days)}
            </PreviewBadge>
            {item.min_stock_amount != null && item.min_stock_amount !== 0 && (
              <PreviewBadge variant="outline" color="gray">
                min stock: {item.min_stock_amount} {stockQuName}
              </PreviewBadge>
            )}
            {purchaseQuName && purchaseQuName !== stockQuName && (
              <PreviewBadge variant="outline" color="gray">
                purchased in {purchaseQuName}
              </PreviewBadge>
            )}
            {consumeQuName && consumeQuName !== stockQuName && (
              <PreviewBadge variant="outline" color="gray">
                consumed in {consumeQuName}
              </PreviewBadge>
            )}
            {item.parent_product != null && (
              <PreviewBadge variant="outline" color="gray">
                variant of {resolveName(reference?.products, item.parent_product)}
              </PreviewBadge>
            )}
          </Group>
          {item.description && <PreviewText c="dimmed">{item.description}</PreviewText>}
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
    <PreviewText>
      <PreviewText span c="dimmed">
        {change.label}:{" "}
      </PreviewText>
      {change.old !== null && (
        <PreviewText span c="dimmed">
          {change.old || "(empty)"} →{" "}
        </PreviewText>
      )}
      <PreviewText span>{change.next}</PreviewText>
    </PreviewText>
  );
}

function StockEntryEditRow({
  item,
  reference,
  variant,
}: {
  item: StockEntryEditItem;
  reference: GrocyReferenceData | null;
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
      <PreviewTitle>Stock entry #{item.entry_id}</PreviewTitle>
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

function StockGetPreview({ args }: PreviewProps<StockGetArgs>) {
  const { reference, error } = useGrocyReferenceData();
  const products = (args.products ?? []).map((value) => resolveName(reference?.products, value));
  const locations = (args.locations ?? []).map((value) => resolveName(reference?.locations, value));
  return (
    <Stack gap="xs">
      {products.length === 0 && locations.length === 0 && <PreviewText>All current stock</PreviewText>}
      {products.length > 0 && <Field label="Products">{products.join(", ")}</Field>}
      {locations.length > 0 && <Field label="Locations">{locations.join(", ")}</Field>}
      <GrocyReferenceLoadError error={error} />
    </Stack>
  );
}

function DetailPreview({ detail, noun }: { detail: "brief" | "full"; noun: string }) {
  return (
    <PreviewText>
      {detail === "full" ? `Full ${noun} records` : `${noun[0].toUpperCase()}${noun.slice(1)} names`}
    </PreviewText>
  );
}

function ProductsListPreview({ args }: PreviewProps<ProductsListArgs>) {
  return <DetailPreview detail={args.detail ?? "brief"} noun="product" />;
}

function QuantityUnitsListPreview({ args }: PreviewProps<QuantityUnitsListArgs>) {
  return <DetailPreview detail={args.detail ?? "brief"} noun="quantity unit" />;
}

function GetSystemInfoPreview(_: PreviewProps<GetSystemInfoArgs>) {
  return <PreviewText>Grocy server version and system details</PreviewText>;
}

function ShoppingListItemsRemoveRow({ itemId, reference }: { itemId: number; reference: GrocyReferenceData | null }) {
  const item = resolveShoppingItem(reference?.shopping_list_items, itemId);
  return (
    <Group gap={6}>
      <PreviewText span fw={600}>
        {shoppingItemLabel(item, itemId)}
      </PreviewText>
      {item && (
        <PreviewText span c="dimmed">
          × {formatAmount(item.amount, item.qu_name)}
        </PreviewText>
      )}
    </Group>
  );
}

function ShoppingListItemsRemovePreview({ args, variant }: PreviewProps<ShoppingListItemsRemoveArgs>) {
  return (
    <GrocyItemsPreview
      items={args.item_ids}
      variant={variant}
      gap={4}
      renderRow={(itemId, reference) => <ShoppingListItemsRemoveRow itemId={itemId} reference={reference} />}
    />
  );
}

type NameMap = { id: number; name: string }[] | undefined;

// The old value to show for a field: `null` while the reference loads (so no old side renders),
// else the resolved value or "(none)" when the field is currently unset. Generic over the
// current record (a product or a shopping-list item) — only its presence matters.
function oldValue<T>(current: T | undefined, resolved: string | null): string | null {
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
  reference: GrocyReferenceData | null
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
  reference: GrocyReferenceData | null,
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
  reference: GrocyReferenceData | null;
  variant: PreviewVariant;
}) {
  const current = resolveProduct(reference?.products, item.product);
  const changes = productEditChanges(item, current, reference, variant);
  const shown = variant === "compact" ? changes.slice(0, 2) : changes;
  return (
    <Stack gap={2}>
      <PreviewTitle>{resolveName(reference?.products, item.product)}</PreviewTitle>
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

function ShoppingListGetPreview({ args }: PreviewProps<ShoppingListGetArgs>) {
  const { reference, error } = useGrocyReferenceData();
  return (
    <Stack gap="xs">
      <PreviewText>
        <PreviewText span c="dimmed">
          List{" "}
        </PreviewText>
        <PreviewText span fw={600}>
          {resolveName(reference?.shopping_lists, args.shopping_list)}
        </PreviewText>
      </PreviewText>
      <GrocyReferenceLoadError error={error} />
    </Stack>
  );
}

function ShoppingListAddRow({ item, reference }: { item: ShoppingItem; reference: GrocyReferenceData | null }) {
  return (
    <Group gap={6}>
      {item.product != null ? (
        <Group gap={4}>
          <PreviewText span fw={600}>
            {resolveName(reference?.products, item.product)}
          </PreviewText>
          {item.amount != null && item.amount !== 1 && (
            <PreviewText span c="dimmed">
              × {item.amount}
            </PreviewText>
          )}
        </Group>
      ) : (
        <PreviewText span fw={600}>
          {item.note}
        </PreviewText>
      )}
      <PreviewText span c="dimmed">
        → {resolveName(reference?.shopping_lists, item.shopping_list)}
      </PreviewText>
      {item.product != null && item.note && (
        <PreviewText span c="dimmed">
          ({item.note})
        </PreviewText>
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

// `shopping_list_item_edit` keys off a bare `item_id`; the reference carries each item's current
// amount/note/done, so — like `productEditChanges` — we render an old→new diff. While the
// reference loads (or the item is unknown) `current` is undefined and only the new side shows.
function shoppingItemEditChanges(
  args: ShoppingListItemEditArgs,
  current: ShoppingListRefItem | undefined
): FieldChange[] {
  const unit = current?.qu_name ?? null;
  return [
    args.amount != null
      ? {
          key: "amount",
          label: "amount",
          old: oldValue(current, current ? formatAmount(current.amount, current.qu_name) : null),
          next: formatAmount(args.amount, unit),
        }
      : null,
    args.note != null
      ? { key: "note", label: "note", old: oldValue(current, current?.note ?? null), next: args.note }
      : null,
    args.done != null
      ? {
          key: "done",
          label: "done",
          old: oldValue(current, current ? (current.done ? "yes" : "no") : null),
          next: args.done ? "yes" : "no",
        }
      : null,
    // `EditShoppingListField` is just `note` today; resolve that one's old value, leave any
    // future field's old side blank until this grows an arm for it.
    ...(args.clear_fields ?? []).map(
      (field): FieldChange => ({
        key: `clear_${field}`,
        label: field.replaceAll("_", " "),
        old: field === "note" ? oldValue(current, current?.note ?? null) : null,
        next: CLEARED,
      })
    ),
  ].filter((c): c is FieldChange => c != null);
}

function ShoppingListItemEditPreview({ args, variant }: PreviewProps<ShoppingListItemEditArgs>) {
  const { reference, error } = useGrocyReferenceData();
  const current = resolveShoppingItem(reference?.shopping_list_items, args.item_id);
  const changes = shoppingItemEditChanges(args, current);
  const shown = variant === "compact" ? changes.slice(0, 2) : changes;
  return (
    <Stack gap={2}>
      <PreviewTitle>{shoppingItemLabel(current, args.item_id)}</PreviewTitle>
      <Stack gap={2}>
        {shown.map((change) => (
          <ChangeLine key={change.key} change={change} />
        ))}
        <MoreLine count={changes.length - shown.length} />
      </Stack>
      <GrocyReferenceLoadError error={error} />
    </Stack>
  );
}

/** Per-tool preview widgets for the `grocy-sf` server's stock, product, and shopping-list tools. */
export const grocyPreviews = {
  stock_add: definePreview(zStockAddArgs, StockAddPreview),
  stock_consume: definePreview(zStockConsumeArgs, StockConsumePreview),
  stock_entry_edit: definePreview(zStockEntryEditArgs, StockEntryEditPreview),
  stock_get: definePreview(zStockGetArgs, StockGetPreview),
  products_list: definePreview(zProductsListArgs, ProductsListPreview),
  quantity_units_list: definePreview(zQuantityUnitsListArgs, QuantityUnitsListPreview),
  get_system_info: definePreview(zGetSystemInfoArgs, GetSystemInfoPreview),
  products_create: definePreview(zProductsCreateArgs, ProductsCreatePreview),
  products_edit: definePreview(zProductsEditArgs, ProductsEditPreview),
  shopping_list_get: definePreview(zShoppingListGetArgs, ShoppingListGetPreview),
  shopping_list_items_add: definePreview(zShoppingListItemsAddArgs, ShoppingListItemsAddPreview),
  shopping_list_items_remove: definePreview(zShoppingListItemsRemoveArgs, ShoppingListItemsRemovePreview),
  shopping_list_item_edit: definePreview(zShoppingListItemEditArgs, ShoppingListItemEditPreview),
} satisfies Record<string, ToolPreview>;
