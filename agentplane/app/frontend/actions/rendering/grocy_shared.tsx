import { Anchor, Code, Text } from "@mantine/core";
import { createContext, type JSX, type ReactNode, useContext } from "react";
import { useQuery } from "@tanstack/react-query";
import { z } from "zod";

import { getGrocyReferenceNames } from "../client";
import { definePreview, type PreviewProps } from "./entry";
import type { ActionOutcome, ActionPresentation } from "./presentation";
import type { ResultPreview } from "./result_entry";

export const GROCY_BASE: string = "https://grocy-sf.allegedly.works";
export const reference: z.ZodType<string | number> = z.union([z.number().int(), z.string()]);

export function Line({ children }: { children: ReactNode }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>
      {children}
    </Text>
  );
}

export function objectLink(path: string, id: number, name: string): JSX.Element {
  return (
    <Anchor href={`${GROCY_BASE}/${path}/${id}`} target="_blank" rel="noreferrer" inherit>
      {name}
    </Anchor>
  );
}

const review = createContext<string | undefined>(undefined);
const referenceRoutes = {
  products: ["product", "Product"],
  locations: ["location", "Location"],
  quantity_units: ["quantityunit", "Quantity unit"],
  product_groups: ["productgroup", "Product group"],
  shopping_lists: ["shoppinglist", "Shopping list"],
  stock_entries: ["stockentry", "Stock entry"],
} as const;

export type GrocyReferenceKind = keyof typeof referenceRoutes;

export function GrocyReview({ requestId, children }: { requestId?: string; children: ReactNode }): JSX.Element {
  return <review.Provider value={requestId}>{children}</review.Provider>;
}

function GrocyReference({ kind, id }: { kind: GrocyReferenceKind; id: number }): JSX.Element {
  const requestId = useContext(review);
  const query = useQuery({
    queryKey: ["action-integration-preview", "grocy_sf", requestId],
    queryFn: ({ signal }) => getGrocyReferenceNames(requestId ?? "", signal),
    enabled: Boolean(requestId),
    retry: false,
    staleTime: 30_000,
  });
  const [path, fallback] = referenceRoutes[kind];
  return objectLink(path, id, query.data?.get(`${kind}/${id}`) ?? `${fallback} #${id}`);
}

export function displayRef(value: string | number, kind?: GrocyReferenceKind): ReactNode {
  if (typeof value === "string") return value;
  return kind ? <GrocyReference kind={kind} id={value} /> : <Code>{value}</Code>;
}

export const batchError: z.ZodType<{ kind: "error"; error: string }> = z.strictObject({
  kind: z.literal("error"),
  error: z.string(),
});
export function batchOutcome(rows: { kind: "ok" | "error" }[]): ActionOutcome | undefined {
  const failures = rows.filter((row) => row.kind === "error").length;
  return failures === 0
    ? undefined
    : { label: failures === rows.length ? "All items failed" : "Partially completed", failed: true };
}

export function grocyRequest<S extends z.ZodType>(
  label: string,
  schema: S,
  preview: (props: PreviewProps<z.infer<S>>) => ReactNode,
  result: ResultPreview
): ActionPresentation {
  const Preview = preview;
  const argumentsPreview = definePreview(schema, ({ args, requestId }) => (
    <GrocyReview requestId={requestId}>
      <Preview args={args} requestId={requestId} />
    </GrocyReview>
  ));
  return {
    fallbackLabel: label,
    pane: { opened: argumentsPreview },
    details: { arguments: argumentsPreview, result, pendingPreviewOnly: true },
  };
}
