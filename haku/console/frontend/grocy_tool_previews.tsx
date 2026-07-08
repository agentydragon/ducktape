// Per-tool-type rendering for the remote `grocy-sf` MCP server (see
// grocy_mcp/README.md and grocy_mcp/batch_tools.py). Falls back to the generic raw-JSON
// view for anything that isn't shaped as expected — same caveat as kubectl_tool_previews.tsx:
// arguments are only validated by the tool's own schema at execution time, not at submission.
//
// grocy-sf's tool surface is generated from Grocy's own OpenAPI spec plus custom batch
// tools (grocy_mcp/batch_tools.py) — there's no backend Pydantic model haku-console owns to
// generate Zod schemas from (unlike google_tool_previews.tsx's :schema_zod), so these are
// hand-authored once, here, against grocy_mcp/mcp_types.py's `AddItem` / `ConsumeItem` /
// `CreateProductItem`. Every tool call runs as the approving operator's own linked Grocy
// account (operator_oauth) once approved.

import { Badge, Group, Stack, Text } from "@mantine/core";
import type { ReactNode } from "react";
import { z } from "zod";

import { Field } from "./field.tsx";

const GROCY_SERVER_ID = "grocy-sf";

// `int | str` on the Python side (name or ID) — render whichever the caller passed.
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

function ProductAmount({ product, amount, qu }: { product: string | number; amount: number; qu: string | number }) {
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

function StockAddRow({ item }: { item: AddItem }) {
  return (
    <Group gap={6}>
      <ProductAmount product={item.product} amount={item.amount} qu={item.qu} />
      <Text span c="dimmed">
        → {item.location}
      </Text>
      {item.best_before_date && (
        <Badge size="sm" variant="outline">
          best before {item.best_before_date}
        </Badge>
      )}
    </Group>
  );
}

function StockAddPreview({ args }: { args: StockAddArgs }) {
  return (
    <Stack gap="xs">
      <Field label="Action">
        <Badge color="green" variant="light">
          Add stock
        </Badge>
      </Field>
      <Stack gap={4}>
        {args.items.map((item, i) => (
          <StockAddRow key={i} item={item} />
        ))}
      </Stack>
    </Stack>
  );
}

function StockConsumeRow({ item }: { item: ConsumeItem }) {
  return (
    <Group gap={6}>
      <ProductAmount product={item.product} amount={item.amount} qu={item.qu} />
      <Text span c="dimmed">
        from {item.location}
      </Text>
      {item.spoiled && (
        <Badge size="sm" color="orange" variant="outline">
          spoiled
        </Badge>
      )}
    </Group>
  );
}

function StockConsumePreview({ args }: { args: StockConsumeArgs }) {
  return (
    <Stack gap="xs">
      <Field label="Action">
        <Badge color="red" variant="light">
          Remove stock
        </Badge>
      </Field>
      <Stack gap={4}>
        {args.items.map((item, i) => (
          <StockConsumeRow key={i} item={item} />
        ))}
      </Stack>
    </Stack>
  );
}

function ProductsCreateRow({ item }: { item: CreateProductItem }) {
  return (
    <Group gap={6}>
      <Text span fw={600}>
        {item.name}
      </Text>
      <Text span c="dimmed">
        stock in {item.stock_qu}, at {item.location}
      </Text>
      {item.product_group && <Badge size="sm">{item.product_group}</Badge>}
    </Group>
  );
}

function ProductsCreatePreview({ args }: { args: ProductsCreateArgs }) {
  return (
    <Stack gap="xs">
      <Field label="Action">
        <Badge color="blue" variant="light">
          Create product{args.items.length > 1 ? "s" : ""}
        </Badge>
      </Field>
      <Stack gap={4}>
        {args.items.map((item, i) => (
          <ProductsCreateRow key={i} item={item} />
        ))}
      </Stack>
    </Stack>
  );
}

/** Nice per-tool rendering for the `grocy-sf` server's stock and product-creation tools;
 * `null` for anything else, so the caller falls back to raw JSON. */
export function grocyToolPreview(serverId: string, toolName: string, args: Record<string, unknown>): ReactNode | null {
  if (serverId !== GROCY_SERVER_ID) return null;
  if (toolName === "stock_add") {
    const parsed = zStockAddArgs.safeParse(args);
    return parsed.success && <StockAddPreview args={parsed.data} />;
  }
  if (toolName === "stock_consume") {
    const parsed = zStockConsumeArgs.safeParse(args);
    return parsed.success && <StockConsumePreview args={parsed.data} />;
  }
  if (toolName === "products_create") {
    const parsed = zProductsCreateArgs.safeParse(args);
    return parsed.success && <ProductsCreatePreview args={parsed.data} />;
  }
  return null;
}
