import { Stack } from "@mantine/core";
import type { JSX } from "react";
import { z } from "zod";

import { defineCallPreview } from "./entry";
import {
  batchError,
  batchOutcome,
  displayRef,
  grocyRequest,
  Line,
  objectLink,
  reference,
  useGrocyReferences,
} from "./grocy_shared";
import { type ActionPresentation, expandsForRejectedArguments } from "./presentation";
import { defineResultPreview, type ResultPreview } from "./result_entry";

const id = z.number().int();
const text = z.string().nullish();
const listArguments = z.strictObject({ detail: z.enum(["brief", "full"]).optional() });
const selectList = z.strictObject({ shopping_list: reference });
const getList = z.object({
  name: z.string(),
  description: text,
  items: z.array(
    z.strictObject({
      item_id: id,
      product_name: text,
      product_id: id.nullish(),
      amount: z.number(),
      qu_name: text,
      note: text,
      done: z.boolean(),
    })
  ),
});
const listItems = defineResultPreview(getList, ({ result }) => (
  <Stack gap="xs">
    <Line>
      <strong>{result.name}</strong> ·{" "}
      {result.items.length === 0 ? "Empty" : `${result.items.length} ${result.items.length === 1 ? "item" : "items"}`}
    </Line>
    {result.description && <Line>{result.description}</Line>}
    {result.items.map((item) => (
      <Stack gap={2} key={item.item_id}>
        <Line>
          {objectLink("shoppinglistitem", item.item_id, item.product_name || item.note || "Unnamed item")}
          {item.done && " · Done"}
          {item.product_name && ` · ${item.amount}`}
          {item.qu_name && ` ${item.qu_name}`}
        </Line>
        {item.product_name && item.note && <Line>{item.note}</Line>}
      </Stack>
    ))}
  </Stack>
));

function shoppingAmount(amount: number, unit?: string): string {
  return unit ? `${amount} ${unit}` : String(amount);
}

function shoppingItemChange(before: string | undefined, after: string): string {
  return before === undefined ? after : `${before} → ${after}`;
}

function ShoppingTarget({
  id,
  showAmount = false,
  recordedName,
}: {
  id: number;
  showAmount?: boolean;
  recordedName?: string;
}): JSX.Element {
  const item = useGrocyReferences()?.get(`shopping_list/${id}`);
  const current = item?.shoppingItemCurrent;
  return (
    <Line>
      {objectLink("shoppinglistitem", id, item?.name || recordedName || `Shopping item #${id}`)}
      {current?.listName && ` · ${current.listName}`}
      {showAmount && current?.amount !== undefined && ` · ${shoppingAmount(current.amount, current.unitName)}`}
    </Line>
  );
}

const editItem = z.strictObject({
  item_id: id,
  amount: z.number().nullish(),
  note: text,
  done: z.boolean().nullish(),
  clear_fields: z.array(z.literal("note")).nullish(),
});
function ShoppingEdit({ args, recordedName }: { args: z.infer<typeof editItem>; recordedName?: string }): JSX.Element {
  const current = useGrocyReferences()?.get(`shopping_list/${args.item_id}`)?.shoppingItemCurrent;
  const clearNote = args.clear_fields?.includes("note");
  const oldNote = current && (current.note === undefined ? "None" : current.note || "(empty text)");
  return (
    <Stack gap={4}>
      <ShoppingTarget id={args.item_id} recordedName={recordedName} />
      {args.amount != null && (
        <Line>
          Quantity ·{" "}
          {shoppingItemChange(
            current?.amount === undefined ? undefined : shoppingAmount(current.amount, current.unitName),
            shoppingAmount(args.amount, current?.unitName)
          )}
        </Line>
      )}
      {args.done != null && (
        <Line>
          {current?.done === undefined
            ? args.done
              ? "Mark done"
              : "Mark not done"
            : shoppingItemChange(current.done ? "Done" : "Not done", args.done ? "Done" : "Not done")}
        </Line>
      )}
      {(clearNote || args.note != null) && (
        <Line>Note · {shoppingItemChange(oldNote, clearNote ? "Clear" : args.note || "(empty text)")}</Line>
      )}
    </Stack>
  );
}

const addItems = z.strictObject({
  items: z.array(
    z
      .strictObject({
        shopping_list: reference,
        product: reference.nullish(),
        amount: z.number().optional(),
        note: text,
      })
      .refine((item) => item.product != null || item.note != null)
  ),
});
const removeItems = z.strictObject({ item_ids: z.array(id) });

const shoppingItemOk = z.strictObject({
  kind: z.literal("ok"),
  item_id: id,
  product_name: text,
  amount: z.number(),
  qu_name: text,
});
const itemOutcome = z.union([shoppingItemOk, batchError]);
const itemOutcomes = z.array(itemOutcome);

function itemReceipt(verb: string, rows: z.infer<typeof itemOutcomes>) {
  const count = rows.filter((item) => item.kind === "ok").length;
  return (
    <Stack gap={4}>
      <Line>
        {count} {count === 1 ? "item" : "items"} {verb}
      </Line>
      {rows.map((row, i) => (
        <Line key={i}>
          {row.kind === "error" ? (
            <>Failed · {row.error}</>
          ) : (
            <>
              {verb === "removed"
                ? row.product_name || "Note-only item"
                : objectLink("shoppinglistitem", row.item_id, row.product_name || "Note-only item")}
              {row.product_name && ` · ${row.amount}`}
              {row.qu_name && ` ${row.qu_name}`}
            </>
          )}
        </Line>
      ))}
    </Stack>
  );
}

function itemBatchReceipt(verb: string): ResultPreview {
  return defineResultPreview(itemOutcomes, ({ result }) => itemReceipt(verb, result));
}

function itemBatchOutcome(value: unknown) {
  const parsed = itemOutcomes.safeParse(value);
  return parsed.success ? batchOutcome(parsed.data) : undefined;
}

const editedItem = defineResultPreview(itemOutcome, ({ result }) => itemReceipt("updated", [result]));
const editShoppingItemCall = defineCallPreview(editItem, itemOutcome, ({ args, result }) => {
  if (!result) return null;
  const matching = result.kind === "ok" && result.item_id === args.item_id ? result : undefined;
  return (
    <Stack gap="xs">
      <ShoppingEdit args={args} recordedName={matching ? matching.product_name || "Note-only item" : undefined} />
      {matching ? (
        <Line>1 item updated · {shoppingAmount(matching.amount, matching.qu_name ?? undefined)}</Line>
      ) : (
        itemReceipt("updated", [result])
      )}
    </Stack>
  );
});
const cleared = defineResultPreview(z.strictObject({ kind: z.literal("ok"), cleared: id }), ({ result }) => (
  <Line>
    {result.cleared} {result.cleared === 1 ? "item" : "items"} removed · Shopping list kept
  </Line>
));

const shoppingLists = defineResultPreview(
  z.array(
    z.object({
      id,
      name: z.string(),
      description: text,
    })
  ),
  ({ result }) => (
    <Stack gap={4}>
      {result.length === 0 && <Line>No shopping lists</Line>}
      {result.map((list) => (
        <Line key={list.id}>
          {objectLink("shoppinglist", list.id, list.name)}
          {list.description && ` · ${list.description}`}
        </Line>
      ))}
    </Stack>
  )
);
const createLists = z.strictObject({ items: z.array(z.strictObject({ name: z.string(), description: text })) });
const createdList = z.strictObject({ kind: z.literal("ok"), created_object_id: id.nullish() });
const createdLists = z.array(z.union([createdList, batchError]));
function createdReceipt(rows: z.infer<typeof createdLists>, names?: readonly string[]) {
  return (
    <Stack gap={4}>
      {rows.length === 0 && <Line>No shopping list results</Line>}
      {rows.map((row, i) => (
        <Line key={i}>
          {row.kind === "error" ? (
            <>
              Failed{names?.[i] && ` · ${names[i]}`} · {row.error}
            </>
          ) : (
            <>
              Created{" "}
              {row.created_object_id != null
                ? objectLink("shoppinglist", row.created_object_id, names?.[i] ?? "shopping list")
                : (names?.[i] ?? "shopping list")}
            </>
          )}
        </Line>
      ))}
    </Stack>
  );
}

function NewLists({ args }: { args: z.infer<typeof createLists> }) {
  return (
    <Stack gap={4}>
      {args.items.map((item, i) => (
        <Line key={i}>
          <strong>{item.name}</strong>
          {item.description && ` · ${item.description}`}
        </Line>
      ))}
    </Stack>
  );
}

export const grocyShoppingRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "shopping_list_get",
    grocyRequest(
      "View Grocy shopping list",
      selectList,
      ({ args }) => <Line>{displayRef(args.shopping_list, "shopping_lists")}</Line>,
      listItems
    ),
  ],
  [
    "shopping_list_clear",
    grocyRequest(
      "Empty Grocy shopping list",
      selectList,
      ({ args }) => (
        <Line>Remove every item from {displayRef(args.shopping_list, "shopping_lists")} and keep the list.</Line>
      ),
      cleared
    ),
  ],
  [
    "shopping_list_items_add",
    {
      ...grocyRequest(
        "Add Grocy shopping items",
        addItems,
        ({ args }) => (
          <Stack gap="xs">
            {args.items.map((item, i) => (
              <Stack key={i} gap={2}>
                <Line>
                  <strong>{item.product == null ? item.note : displayRef(item.product, "products")}</strong> ·{" "}
                  {item.amount ?? 1} for {displayRef(item.shopping_list, "shopping_lists")}
                </Line>
                {item.product != null && item.note != null && <Line>Note · {item.note || "(empty text)"}</Line>}
              </Stack>
            ))}
          </Stack>
        ),
        itemBatchReceipt("added")
      ),
      outcome: itemBatchOutcome,
    },
  ],
  [
    "shopping_list_items_remove",
    {
      ...grocyRequest(
        "Remove Grocy shopping items",
        removeItems,
        ({ args }) => (
          <Stack gap={4}>
            <Line>
              {args.item_ids.length} selected {args.item_ids.length === 1 ? "item" : "items"}
            </Line>
            {args.item_ids.map((itemId, i) => (
              <ShoppingTarget key={i} id={itemId} showAmount />
            ))}
          </Stack>
        ),
        itemBatchReceipt("removed")
      ),
      outcome: itemBatchOutcome,
    },
  ],
  [
    "shopping_list_item_edit",
    {
      ...grocyRequest("Edit Grocy shopping item", editItem, ShoppingEdit, editedItem),
      call: editShoppingItemCall,
      outcome: (value) => {
        const parsed = itemOutcome.safeParse(value);
        return parsed.success ? batchOutcome([parsed.data]) : undefined;
      },
    },
  ],
  [
    "shopping_lists_list",
    {
      ...grocyRequest("List Grocy shopping lists", listArguments, () => null, shoppingLists),
      compactCardExpands: expandsForRejectedArguments(listArguments),
    },
  ],
  [
    "shopping_lists_create",
    {
      ...grocyRequest(
        "Create Grocy shopping lists",
        createLists,
        NewLists,
        defineResultPreview(createdLists, ({ result }) => createdReceipt(result))
      ),
      call: defineCallPreview(createLists, createdLists, ({ args, result }) => (
        <Stack gap="sm">
          <NewLists args={args} />
          {result &&
            createdReceipt(
              result,
              result.length === args.items.length ? args.items.map((item) => item.name) : undefined
            )}
        </Stack>
      )),
      outcome: (value) => {
        const parsed = createdLists.safeParse(value);
        return parsed.success ? batchOutcome(parsed.data) : undefined;
      },
    },
  ],
]);
