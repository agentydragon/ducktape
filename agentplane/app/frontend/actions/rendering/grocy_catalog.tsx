import { Stack } from "@mantine/core";
import type { ReactNode } from "react";
import { z } from "zod";

import { defineCallPreview } from "./entry";
import { batchError, batchOutcome, displayRef, grocyRequest, Line, objectLink, reference } from "./grocy_shared";
import { type ActionPresentation, expandsForRejectedArguments } from "./presentation";
import { defineResultPreview } from "./result_entry";

const id = z.number().int();
const text = z.string().nullish();
const named = z.strictObject({ name: z.string(), description: text });
const listArgs = z.strictObject({ detail: z.enum(["brief", "full"]).optional() });
const entryRow = z.object({
  id,
  name: z.string(),
  description: text,
  is_freezer: z.union([z.boolean(), z.literal(1), z.literal(0), z.literal("1"), z.literal("0")]).nullish(),
});
const product = named.extend({
  stock_qu: reference,
  location: reference,
  default_best_before_days: z.number().int(),
  purchase_qu: reference.nullish(),
  consume_qu: reference.nullish(),
  parent_product: reference.nullish(),
  product_group: reference.nullish(),
  min_stock_amount: z.number().optional(),
  due_type: z.union([z.literal(1), z.literal(2)]).optional(),
});
const location = named.extend({ is_freezer: z.boolean().optional() });
const quantityUnit = named.extend({ name_plural: z.string(), plural_forms: text });
const createResult = z.array(
  z.union([z.strictObject({ kind: z.literal("ok"), created_object_id: id.nullish() }), batchError])
);

function lists(label: string, plural: string, path: string): ActionPresentation {
  return {
    ...grocyRequest(
      label,
      listArgs,
      () => null,
      defineResultPreview(z.array(entryRow), ({ result }) => (
        <Stack gap={4}>
          {result.length === 0 && <Line>No {plural}</Line>}
          {result.map((row) => (
            <Line key={row.id}>
              {objectLink(path, row.id, row.name)}
              {(row.is_freezer === true || row.is_freezer === 1 || row.is_freezer === "1") && " · Freezer"}
              {row.description && ` · ${row.description}`}
            </Line>
          ))}
        </Stack>
      ))
    ),
    compactCardExpands: expandsForRejectedArguments(listArgs),
  };
}

function createView<S extends z.ZodType<{ name: string; description?: string | null }>>(
  label: string,
  schema: S,
  path: string,
  details?: (item: z.infer<S>) => ReactNode
): ActionPresentation {
  const argsSchema = z.strictObject({ items: z.array(schema) });
  const request = ({ args }: { args: z.infer<typeof argsSchema> }) => (
    <Stack gap="xs">
      {args.items.map((item, i) => (
        <Stack gap={2} key={i}>
          <Line>
            <strong>{item.name}</strong>
          </Line>
          {item.description != null && <Line>{item.description || "(empty description)"}</Line>}
          {details?.(item)}
        </Stack>
      ))}
    </Stack>
  );
  const receipt = (results: z.infer<typeof createResult>, names?: string[]) => (
    <Stack gap={4}>
      {results.length === 0 && <Line>No creation results</Line>}
      {results.map((row, i) => (
        <Line key={i}>
          {row.kind === "error" ? (
            <>
              Failed{names?.[i] && ` · ${names[i]}`} · {row.error}
            </>
          ) : (
            <>
              Created{" "}
              {row.created_object_id == null
                ? (names?.[i] ?? "record")
                : objectLink(path, row.created_object_id, names?.[i] ?? "record")}
            </>
          )}
        </Line>
      ))}
    </Stack>
  );
  return {
    ...grocyRequest(
      label,
      argsSchema,
      request,
      defineResultPreview(createResult, ({ result }) => receipt(result))
    ),
    call: defineCallPreview(argsSchema, createResult, ({ args, result }) => (
      <Stack gap="sm">
        {request({ args })}
        {result &&
          receipt(result, result.length === args.items.length ? args.items.map((item) => item.name) : undefined)}
      </Stack>
    )),
    outcome: (value) => {
      const parsed = createResult.safeParse(value);
      return parsed.success ? batchOutcome(parsed.data) : undefined;
    },
  };
}

const removeProduct = z.strictObject({ product: reference });
const deletion = z.union([z.strictObject({ kind: z.literal("ok"), object_id: id }), batchError]);
function deletionReceipt(result: z.infer<typeof deletion>, product?: string | number) {
  return result.kind === "ok" ? (
    <Line>Deleted {typeof product === "string" ? product : "product"} and its stock entries</Line>
  ) : (
    <Line>Delete failed · {result.error}</Line>
  );
}
function deletionRequest({ args }: { args: z.infer<typeof removeProduct> }) {
  return (
    <Line>
      Permanently delete{" "}
      {typeof args.product === "number"
        ? objectLink("product", args.product, `Product #${args.product}`)
        : args.product}{" "}
      and every stock entry for it.
    </Line>
  );
}

const merge = z.strictObject({ productIdToKeep: id, productIdToRemove: id });
const merged = defineResultPreview(z.null(), () => <Line>Products merged</Line>);

export const grocyCatalogRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "products_create",
    createView("Create Grocy products", product, "product", (item) => (
      <>
        <Line>Default location · {displayRef(item.location)}</Line>
        <Line>Stock unit · {displayRef(item.stock_qu)}</Line>
        {item.purchase_qu != null && (
          <Line>
            Purchase unit · {displayRef(item.purchase_qu)}
            {item.purchase_qu !== item.stock_qu && " · May create a 1:1 conversion"}
          </Line>
        )}
        {item.consume_qu != null && <Line>Consume unit · {displayRef(item.consume_qu)}</Line>}
        <Line>
          Default shelf life ·{" "}
          {item.default_best_before_days === -1 ? "Never expires" : `${item.default_best_before_days} days`} ·{" "}
          {item.due_type === 2 ? "Expiration" : "Best before"}
        </Line>
        {item.min_stock_amount !== undefined && item.min_stock_amount > 0 && (
          <Line>
            Minimum stock · {item.min_stock_amount} {displayRef(item.stock_qu)}
          </Line>
        )}
        {item.product_group != null && <Line>Product group · {displayRef(item.product_group)}</Line>}
        {item.parent_product != null && <Line>Variant of · {displayRef(item.parent_product)}</Line>}
      </>
    )),
  ],
  [
    "product_delete",
    {
      ...grocyRequest(
        "Delete Grocy product",
        removeProduct,
        deletionRequest,
        defineResultPreview(deletion, ({ result }) => deletionReceipt(result))
      ),
      call: defineCallPreview(removeProduct, deletion, ({ args, result }) => (
        <Stack gap={4}>
          {deletionRequest({ args })}
          {result && deletionReceipt(result, args.product)}
        </Stack>
      )),
      outcome: (value) => {
        const parsed = deletion.safeParse(value);
        return parsed.success && parsed.data.kind === "error" ? { label: "Delete failed", failed: true } : undefined;
      },
    },
  ],
  [
    "products_merge",
    grocyRequest(
      "Merge Grocy products",
      merge,
      ({ args }) => (
        <Stack gap={4}>
          <Line>Keep {objectLink("product", args.productIdToKeep, `Product #${args.productIdToKeep}`)}</Line>
          <Line>
            Merge and remove {objectLink("product", args.productIdToRemove, `Product #${args.productIdToRemove}`)}
          </Line>
        </Stack>
      ),
      merged
    ),
  ],
  [
    "locations_create",
    createView(
      "Create Grocy locations",
      location,
      "location",
      (item) => item.is_freezer && <Line>Freezer · Adjusts due dates when stock moves in or out</Line>
    ),
  ],
  ["locations_list", lists("List Grocy storage locations", "storage locations", "location")],
  ["product_groups_create", createView("Create Grocy product groups", named, "productgroup")],
  ["product_groups_list", lists("List Grocy product groups", "product groups", "productgroup")],
  [
    "quantity_units_create",
    createView("Create Grocy quantity units", quantityUnit, "quantityunit", (item) => (
      <>
        <Line>Plural · {item.name_plural}</Line>
        {item.plural_forms && <Line>Plural rule · {item.plural_forms}</Line>}
      </>
    )),
  ],
]);
