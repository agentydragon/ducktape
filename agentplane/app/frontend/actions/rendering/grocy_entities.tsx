import { Anchor, Stack } from "@mantine/core";
import type { ReactNode } from "react";
import { z } from "zod";

import { JsonBlock } from "../../ui/text_block";
import { defineCallPreview, type PreviewProps } from "./entry";
import { batchError, batchOutcome, displayRef, GROCY_BASE, grocyRequest, Line, objectLink } from "./grocy_shared";
import { type ActionPresentation, expandsForRejectedArguments } from "./presentation";
import { defineResultPreview, type ResultPreview } from "./result_entry";

const entity = z.string().regex(/^[a-z][a-z0-9_]*$/);
const id = z.number().int();
const apiId = z.union([id, z.string().regex(/^\d+$/).transform(Number)]);
const record = z.record(z.string(), z.unknown());
const target = z.strictObject({ entity, objectId: id });
const update = target.extend({ body: record });
const create = z.strictObject({ items: z.array(z.strictObject({ entity_type: entity, body: record })) });
const get = z.strictObject({ entity_type: entity, object_ids: z.array(id) });
const list = z.strictObject({ entity_types: z.array(entity) });

const route: Readonly<Record<string, string>> = {
  products: "product",
  locations: "location",
  shopping_locations: "shoppinglocation",
  shopping_list: "shoppinglistitem",
  shopping_lists: "shoppinglist",
  quantity_units: "quantityunit",
  quantity_unit_conversions: "quantityunitconversion",
  product_groups: "productgroup",
  product_barcodes: "productbarcodes",
  userfields: "userfield",
  userentities: "userentity",
};
const singular: Readonly<Record<string, string>> = {
  products: "Product",
  locations: "Location",
  shopping_locations: "Shopping location",
  shopping_list: "Shopping item",
  shopping_lists: "Shopping list",
  quantity_units: "Quantity unit",
  quantity_unit_conversions: "Unit conversion",
  product_groups: "Product group",
  product_barcodes: "Product barcode",
  recipes: "Recipe",
  tasks: "Task",
  chores: "Chore",
  batteries: "Battery",
  api_keys: "API key",
};
const fieldLabel: Readonly<Record<string, string>> = {
  description: "Description",
  name_plural: "Plural",
  note: "Note",
  amount: "Quantity",
  min_stock_amount: "Minimum stock",
  location_id: "Location",
  product_id: "Product",
  qu_id_stock: "Stock unit",
  factor: "Conversion factor",
  best_before_date: "Best before",
  purchased_date: "Purchased",
  due_date: "Due date",
  barcode: "Barcode",
  from_qu_id: "From unit",
  to_qu_id: "To unit",
  is_freezer: "Freezer",
  done: "Done",
};
const hiddenField = /(?:password|api_?key|token|secret|^key$)/i;

function entityName(value: string): string {
  return value.replaceAll("_", " ");
}

function textName(body: Record<string, unknown>): string | undefined {
  return [body.name, body.display_name, body.username, body.barcode, body.note].find(
    (value): value is string => typeof value === "string" && value.length > 0
  );
}

function namedTarget(kind: string, id: number): ReactNode {
  switch (kind) {
    case "products":
    case "locations":
    case "quantity_units":
    case "product_groups":
    case "shopping_lists":
      return displayRef(id, kind);
    default:
      return subject(kind, id);
  }
}

function subject(entity: string, objectId?: number, name?: string): ReactNode {
  const title =
    name ??
    (objectId === undefined
      ? (singular[entity] ?? entityName(entity))
      : `${singular[entity] ?? entityName(entity)} #${objectId}`);
  const path = route[entity];
  return path !== undefined && objectId !== undefined ? objectLink(path, objectId, title) : title;
}

function FieldValue({ value }: { value: unknown }) {
  if (value === null) return "(clear)";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return value;
  if (typeof value === "string") return value || "(empty text)";
  return <JsonBlock value={value} />;
}

function WriteFields({ body }: { body: Record<string, unknown> }) {
  return (
    <Stack gap={2}>
      {Object.entries(body).map(([key, value]) =>
        hiddenField.test(key) ? (
          <Line key={key}>{fieldLabel[key] ?? entityName(key)} · Value supplied</Line>
        ) : value !== null && typeof value === "object" ? (
          <Stack gap={2} key={key}>
            <Line>
              <strong>{fieldLabel[key] ?? entityName(key)}</strong>
            </Line>
            <JsonBlock value={value} />
          </Stack>
        ) : (
          <Line key={key}>
            {fieldLabel[key] ?? entityName(key)} · <FieldValue value={value} />
          </Line>
        )
      )}
      {Object.keys(body).length === 0 && <Line>No fields supplied</Line>}
    </Stack>
  );
}

function EntityRow({ kind, data, objectId }: { kind: string; data: Record<string, unknown>; objectId?: number }) {
  const parsed = apiId.safeParse(data.id);
  const id = objectId ?? (parsed.success ? parsed.data : undefined);
  const name = textName(data);
  const fields = Object.keys(fieldLabel).filter(
    (key) => data[key] !== undefined && data[key] !== null && data[key] !== name
  );
  return (
    <Stack gap={2}>
      <Line>
        <strong>{subject(kind, id, name)}</strong>
      </Line>
      {fields.map((key) => {
        const value = data[key];
        return typeof value === "string" || typeof value === "number" || typeof value === "boolean" ? (
          <Line key={key}>
            {fieldLabel[key]} · <FieldValue value={value} />
          </Line>
        ) : null;
      })}
    </Stack>
  );
}

const entries = z.array(
  z.discriminatedUnion("kind", [
    z.strictObject({ kind: z.literal("ok"), entity_type: entity, object_id: id, data: record }),
    z.strictObject({ kind: z.literal("error"), entity_type: entity, object_id: id, error: z.string() }),
  ])
);
const fetchedReceipt = defineResultPreview(entries, ({ result }) => (
  <Stack gap="xs">
    {result.length === 0 && <Line>No records returned</Line>}
    {result.map((row, index) =>
      row.kind === "ok" ? (
        <EntityRow key={index} kind={row.entity_type} data={row.data} objectId={row.object_id} />
      ) : (
        <Line key={index}>
          Failed · {subject(row.entity_type, row.object_id)} · {row.error}
        </Line>
      )
    )}
  </Stack>
));
const entitiesReceipt = defineResultPreview(z.record(entity, z.array(record)), ({ result }) => (
  <Stack gap="xs">
    {Object.keys(result).length === 0 && <Line>No entity groups returned</Line>}
    {Object.entries(result).map(([kind, entries]) => (
      <Stack gap={4} key={kind}>
        <Line>
          <strong>{entityName(kind)}</strong> · {entries.length}
        </Line>
        {entries.map((entry, index) => (
          <EntityRow key={index} kind={kind} data={entry} />
        ))}
      </Stack>
    ))}
  </Stack>
));

const created = z.array(
  z.union([z.strictObject({ kind: z.literal("ok"), created_object_id: id.nullish() }), batchError])
);
function createdRows(results: z.infer<typeof created>, items?: z.infer<typeof create>["items"]) {
  return (
    <Stack gap={4}>
      {results.length === 0 && <Line>No creation results</Line>}
      {results.map((row, index) => {
        const item = items?.[index];
        const name = item ? textName(item.body) : undefined;
        return (
          <Line key={index}>
            {row.kind === "error" ? (
              <>
                Failed{item && ` · ${name ?? entityName(item.entity_type)}`} · {row.error}
              </>
            ) : (
              <>Created {item ? subject(item.entity_type, row.created_object_id ?? undefined, name) : "record"}</>
            )}
          </Line>
        );
      })}
    </Stack>
  );
}
const createdReceipt = defineResultPreview(created, ({ result }) => createdRows(result));

function NewEntities({ args }: { args: z.infer<typeof create> }) {
  return (
    <Stack gap="xs">
      {args.items.map((item, index) => (
        <Stack gap={4} key={index}>
          <Line>
            <strong>{singular[item.entity_type] ?? entityName(item.entity_type)}</strong>
          </Line>
          <WriteFields body={item.body} />
        </Stack>
      ))}
    </Stack>
  );
}

function filePath(args: z.infer<typeof fileTarget>): string {
  return `${GROCY_BASE}/api/files/${encodeURIComponent(args.group)}/${encodeURIComponent(args.fileName)}`;
}

const fileGroups = z.enum(["equipmentmanuals", "recipepictures", "productpictures", "userfiles", "userpictures"]);
const groupLabel: Record<z.infer<typeof fileGroups>, string> = {
  equipmentmanuals: "Equipment manuals",
  recipepictures: "Recipe pictures",
  productpictures: "Product pictures",
  userfiles: "User files",
  userpictures: "User pictures",
};
const base64 = /^[A-Za-z0-9+/_-]+={0,2}$/;
const fileTarget = z.strictObject({ group: fileGroups, fileName: z.string().regex(base64) });
const fileRead = fileTarget.extend({
  force_serve_as: z.literal("picture").optional(),
  best_fit_width: z.number().optional(),
  best_fit_height: z.number().optional(),
});
const fileWrite = fileTarget.extend({ body: z.string().optional() });

function decodedFilename(encoded: string): string {
  try {
    const bytes = Uint8Array.from(atob(encoded.replaceAll("-", "+").replaceAll("_", "/")), (char) =>
      char.charCodeAt(0)
    );
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes) || encoded;
  } catch {
    return encoded;
  }
}

function FileReview({ args }: { args: z.infer<typeof fileRead> }) {
  return (
    <Stack gap={4}>
      <Line>
        <strong>{decodedFilename(args.fileName)}</strong> · {groupLabel[args.group]}
      </Line>
      {args.force_serve_as === "picture" && <Line>Serve as picture</Line>}
      {args.best_fit_width !== undefined && <Line>Fit width · {args.best_fit_width} px</Line>}
      {args.best_fit_height !== undefined && <Line>Fit height · {args.best_fit_height} px</Line>}
    </Stack>
  );
}

const fileBytes = z.string().regex(base64);
const returnedFile = defineResultPreview(fileBytes, () => <Line>File contents returned</Line>);
const noArguments = z.strictObject({});
function noInput(label: string, result: ResultPreview): ActionPresentation {
  return {
    ...grocyRequest(label, noArguments, () => null, result),
    compactCardExpands: expandsForRejectedArguments(noArguments),
  };
}

const currentUser = defineResultPreview(
  z
    .object({ display_name: z.string().nullish(), username: z.string().nullish() })
    .refine((row) => Boolean(row.display_name || row.username)),
  ({ result }) => (
    <Line>
      <strong>{result.display_name || result.username}</strong>
      {result.username && result.display_name && result.username !== result.display_name && ` · ${result.username}`}
    </Line>
  )
);
const changedTime = defineResultPreview(z.object({ changed_time: z.string().min(1) }), ({ result }) => (
  <Line>
    Grocy database last changed · <strong>{result.changed_time}</strong>
  </Line>
));

function acknowledged<S extends z.ZodType>(
  label: string,
  schema: S,
  preview: (props: PreviewProps<z.infer<S>>) => ReactNode,
  message: string
): ActionPresentation {
  const presentation = grocyRequest(
    label,
    schema,
    preview,
    defineResultPreview(z.null(), () => <Line>{message}</Line>)
  );
  return { ...presentation, details: { ...presentation.details, emptyResult: <Line>{message}</Line> } };
}

export const grocyEntityRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "entities_create",
    {
      ...grocyRequest("Create Grocy records", create, NewEntities, createdReceipt),
      call: defineCallPreview(create, created, ({ args, result }) => (
        <Stack gap="sm">
          <NewEntities args={args} />
          {result && createdRows(result, result.length === args.items.length ? args.items : undefined)}
        </Stack>
      )),
      outcome: (value) => {
        const parsed = created.safeParse(value);
        return parsed.success ? batchOutcome(parsed.data) : undefined;
      },
    },
  ],
  [
    "entities_get",
    {
      ...grocyRequest(
        "Fetch Grocy records by ID",
        get,
        ({ args }) => (
          <Line>
            {entityName(args.entity_type)} ·{" "}
            {args.object_ids.map((id, i) => (
              <span key={i}>
                {i > 0 && ", "}
                {namedTarget(args.entity_type, id)}
              </span>
            ))}
          </Line>
        ),
        fetchedReceipt
      ),
      outcome: (value) => {
        const parsed = entries.safeParse(value);
        return parsed.success ? batchOutcome(parsed.data) : undefined;
      },
    },
  ],
  [
    "entities_list",
    grocyRequest(
      "List Grocy records by type",
      list,
      ({ args }) => <Line>{args.entity_types.map(entityName).join(", ")}</Line>,
      entitiesReceipt
    ),
  ],
  [
    "entity_delete",
    acknowledged(
      "Delete Grocy record",
      target,
      ({ args }) => <Line>Permanently delete {namedTarget(args.entity, args.objectId)}</Line>,
      "Record deleted"
    ),
  ],
  [
    "entity_update",
    acknowledged(
      "Update Grocy record",
      update,
      ({ args }) => (
        <Stack gap={4}>
          <Line>{namedTarget(args.entity, args.objectId)}</Line>
          <WriteFields body={args.body} />
        </Stack>
      ),
      "Record updated"
    ),
  ],
  [
    "file_get",
    {
      ...grocyRequest("Read Grocy file", fileRead, FileReview, returnedFile),
      call: defineCallPreview(fileRead, fileBytes, ({ args, result }) => (
        <Stack gap={4}>
          <FileReview args={args} />
          {result && (
            <Line>
              File contents returned ·{" "}
              <Anchor href={filePath(args)} target="_blank" rel="noreferrer" inherit>
                Open in Grocy
              </Anchor>
            </Line>
          )}
        </Stack>
      )),
    },
  ],
  [
    "file_upload",
    acknowledged(
      "Upload Grocy file",
      fileWrite,
      ({ args }) => (
        <Stack gap={4}>
          <FileReview args={args} />
          {args.body !== undefined && <Line>File content supplied</Line>}
        </Stack>
      ),
      "File uploaded"
    ),
  ],
  ["get_current_user", noInput("View current Grocy user", currentUser)],
  ["get_db_changed_time", noInput("View Grocy database last change", changedTime)],
]);
