import { Accordion, Anchor, Code, Stack, Text, Title } from "@mantine/core";
import { Fragment } from "react";

import { openLink } from "./bridge.ts";
import { UP_NEXT } from "./constants.ts";
import { countdown } from "./deadline.ts";
import { ItemCard } from "./item_card.tsx";
import { LoadError } from "./load_error.tsx";
import type { Doc } from "./repo.ts";
import type { MetaResponse } from "./types.ts";

const dstr = (v: unknown): string => (typeof v === "string" ? v : "");
const dnum = (v: unknown): number => (typeof v === "number" ? v : 0);

// A parsed items/<slug>.md with the frontmatter fields the board ranks on.
interface Row {
  slug: string;
  title: string;
  value: number;
  status: string;
  deadline: string | null;
  body: string;
}

function toRow(d: Doc): Row {
  return {
    slug: d.path.slice("items/".length).replace(/\.md$/, ""),
    title: dstr(d.data.title),
    value: dnum(d.data.value),
    status: dstr(d.data.status) || "open",
    deadline: dstr(d.data.deadline) || null,
    body: d.body,
  };
}

function statusCounts(rows: Row[]): string {
  const counts: Record<string, number> = {};
  for (const r of rows) counts[r.status] = (counts[r.status] ?? 0) + 1;
  return Object.keys(counts)
    .sort()
    .map((status) => `${status}: ${counts[status]}`)
    .join(" · ");
}

interface InboxProps {
  docItems: Doc[] | null; // null while loading
  meta: MetaResponse | null;
  error: string | null;
  now: number;
  onNavigate?: (path: string) => void;
}

// The triage board: a value-ranked list of items with a time-critical "Due soon" section.
// One surface among several (see App's tabs) — not the whole of haku-ui.
export function InboxView({ docItems, meta, error, now, onNavigate }: InboxProps) {
  if (error) return <LoadError what="items" error={error} />;
  if (!docItems)
    return (
      <Text c="dimmed" my="lg">
        Loading…
      </Text>
    );

  const rows = docItems.map(toRow);
  const card = (r: Row) => (
    <ItemCard
      slug={r.slug}
      title={r.title}
      value={r.value}
      status={r.status}
      deadline={r.deadline}
      body={r.body}
      now={now}
      onNavigate={onNavigate}
    />
  );

  const open = rows.filter((r) => r.status === "open");
  // Time-critical first: anything overdue or within DUE_SOON_DAYS, soonest deadline on top.
  const dueSoon = open
    .filter((r) => r.deadline && countdown(r.deadline, now).urgency !== "later")
    .sort((a, b) => new Date(a.deadline!).getTime() - new Date(b.deadline!).getTime());
  const dueSoonSlugs = new Set(dueSoon.map((r) => r.slug));
  // Everything else stays value-ranked; due-soon rows are pulled out so they appear once.
  const ranked = open.filter((r) => !dueSoonSlugs.has(r.slug)).sort((a, b) => b.value - a.value);
  const upNext = ranked.slice(0, UP_NEXT);
  const backlog = ranked.slice(UP_NEXT);

  const renderRows = (rs: Row[]) => rs.map((r) => <Fragment key={r.slug}>{card(r)}</Fragment>);

  return (
    <Stack gap="md">
      {dueSoon.length > 0 && (
        <div>
          <Title order={2} c="orange" size="h4" mb="xs">
            ⏰ Due soon
          </Title>
          {renderRows(dueSoon)}
        </div>
      )}

      <div>
        <Title order={2} size="h4" mb="xs">
          Up next
        </Title>
        {upNext.length > 0 ? renderRows(upNext) : <Text>No open items.</Text>}
      </div>

      {backlog.length > 0 && (
        <Accordion variant="separated">
          <Accordion.Item value="backlog">
            <Accordion.Control>Backlog — {backlog.length} more open item(s)</Accordion.Control>
            <Accordion.Panel>{renderRows(backlog)}</Accordion.Panel>
          </Accordion.Item>
        </Accordion>
      )}

      <Text size="sm" c="dimmed" mt="xl" pt="md" style={{ borderTop: "1px solid var(--mantine-color-default-border)" }}>
        {open.length} open · {statusCounts(rows)}
        {meta && (
          <>
            <br />
            Last scan:{" "}
            <time dateTime={meta.scan_time} title={meta.scan_time}>
              {new Date(meta.scan_time).toLocaleString()}
            </time>
            {meta.deployed_commit && meta.deployed_commit_url && (
              <>
                {" · deployed "}
                <Anchor inherit onClick={() => void openLink(meta.deployed_commit_url!)} style={{ cursor: "pointer" }}>
                  <Code>{meta.deployed_commit}</Code>
                </Anchor>
              </>
            )}
          </>
        )}
      </Text>
    </Stack>
  );
}
