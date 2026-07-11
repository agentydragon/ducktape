// Per-tool-type rendering for the remote `grocy-sf` MCP server (see
// grocy_mcp/README.md and grocy_mcp/batch_tools.py). Falls back to the generic raw-JSON
// view for anything that isn't shaped as expected — same caveat as kubectl.tsx:
// arguments are only validated by the tool's own schema at execution time, not at submission.
//
// grocy-sf's tool surface is generated from Grocy's own OpenAPI spec plus custom batch
// tools (grocy_mcp/batch_tools.py) — there's no backend Pydantic model haku-console owns to
// generate Zod schemas from (unlike gmail.tsx's :schema_zod), so these are
// hand-authored once, here, against grocy_mcp/mcp_types.py's `AddItem` / `ConsumeItem` /
// `CreateProductItem`. Every tool call runs as the approving operator's own linked Grocy
// account (operator_oauth) once approved.
//
// `product` / `location` / `qu` / `product_group` / `parent_product` arguments accept either
// a name or a numeric ID (grocy_mcp's `EntityResolver` resolves either at execution time); an
// ID alone renders poorly, so `useGrocyReference` fetches `{id, name}` lookups once per widget
// via GET /api/grocy-sf/reference and every row resolves through it.

import { Badge, Group, Stack, Text } from "@mantine/core";
import { useEffect, useState } from "react";
import { z } from "zod";

import { fetchGrocyReference, type GrocyReferenceResponse } from "../grocy_client.ts";
import { definePreview, type ToolPreview } from "./entry.tsx";
import { COMPACT_ITEM_LIMIT, MoreLine, plural, type PreviewProps, type PreviewVariant } from "./variant.tsx";

export const GROCY_SERVER_ID = "grocy-sf";

// `int | str` on the Python side (name or ID) — resolved to a name for display either way.
const zNameOrId = z.union([z.string(), z.number()]);

const zAddItem = z.object({
  product: zNameOrId,
  amount: z.number(),
  qu: zNameOrId,
  location: zNameOrId,
  best_before_date: z.string().nullish(),
  price: z.number().nullish(),
  note: z.string().nullish(),
});

const zConsumeItem = z.object({
  product: zNameOrId,
  amount: z.number(),
  qu: zNameOrId,
  location: zNameOrId,
  spoiled: z.boolean().optional(),
  allow_subproduct_substitution: z.boolean().optional(),
});

const zCreateProductItem = z.object({
  name: z.string(),
  stock_qu: zNameOrId,
  location: zNameOrId,
  purchase_qu: zNameOrId.nullish(),
  min_stock_amount: z.number().optional(),
  consume_qu: zNameOrId.nullish(),
  default_best_before_days: z.number(),
  due_type: z.union([z.literal(1), z.literal(2)]).optional(),
  parent_product: zNameOrId.nullish(),
  product_group: zNameOrId.nullish(),
  description: z.string().nullish(),
});

const zStockAddArgs = z.object({ items: z.array(zAddItem) });
const zStockConsumeArgs = z.object({ items: z.array(zConsumeItem) });
const zProductsCreateArgs = z.object({ items: z.array(zCreateProductItem) });

type AddItem = z.infer<typeof zAddItem>;
type ConsumeItem = z.infer<typeof zConsumeItem>;
type CreateProductItem = z.infer<typeof zCreateProductItem>;
type StockAddArgs = z.infer<typeof zStockAddArgs>;
type StockConsumeArgs = z.infer<typeof zStockConsumeArgs>;
type ProductsCreateArgs = z.infer<typeof zProductsCreateArgs>;

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

function GrocyReferenceLoadError({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <Text size="sm" c="red">
      Couldn't resolve product/location names: {error}
    </Text>
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
  const { reference, error } = useGrocyReference();
  const shown = variant === "compact" ? args.items.slice(0, COMPACT_ITEM_LIMIT) : args.items;
  return (
    <Stack gap="xs">
      <Stack gap={4}>
        {shown.map((item, i) => (
          <StockAddRow key={i} item={item} reference={reference} />
        ))}
        <MoreLine count={args.items.length - shown.length} />
      </Stack>
      <GrocyReferenceLoadError error={error} />
    </Stack>
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
  const { reference, error } = useGrocyReference();
  const shown = variant === "compact" ? args.items.slice(0, COMPACT_ITEM_LIMIT) : args.items;
  return (
    <Stack gap="xs">
      <Stack gap={4}>
        {shown.map((item, i) => (
          <StockConsumeRow key={i} item={item} reference={reference} />
        ))}
        <MoreLine count={args.items.length - shown.length} />
      </Stack>
      <GrocyReferenceLoadError error={error} />
    </Stack>
  );
}

// -1 = never expires, 0 = same-day, N>0 = N days — mirrors DEFAULT_BBD_DESC in
// grocy_mcp/mcp_types.py.
function formatDefaultBestBeforeDays(days: number): string {
  if (days === -1) return "never expires";
  if (days === 0) return "same-day";
  return `${days} day${days === 1 ? "" : "s"}`;
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
  const { reference, error } = useGrocyReference();
  const shown = variant === "compact" ? args.items.slice(0, COMPACT_ITEM_LIMIT) : args.items;
  return (
    <Stack gap="xs">
      <Stack gap={6}>
        {shown.map((item, i) => (
          <ProductsCreateRow key={i} item={item} reference={reference} variant={variant} />
        ))}
        <MoreLine count={args.items.length - shown.length} />
      </Stack>
      <GrocyReferenceLoadError error={error} />
    </Stack>
  );
}

/** Per-tool preview widgets for the `grocy-sf` server's stock and product-creation tools. */
export const grocyPreviews = {
  stock_add: definePreview(zStockAddArgs, StockAddPreview, (a) => ({
    text: `Grocy: Add ${plural(a.items.length, "item")} to stock`,
  })),
  stock_consume: definePreview(zStockConsumeArgs, StockConsumePreview, (a) => ({
    text: `Grocy: Remove ${plural(a.items.length, "item")} from stock`,
  })),
  products_create: definePreview(zProductsCreateArgs, ProductsCreatePreview, (a) => ({
    text: `Grocy: Create ${plural(a.items.length, "product")}`,
  })),
} satisfies Record<string, ToolPreview>;
