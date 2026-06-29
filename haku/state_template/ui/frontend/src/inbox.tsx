import { Accordion, Anchor, Code, Stack, Text, Title } from "@mantine/core";

import { openLink } from "./bridge.ts";
import { UP_NEXT } from "./constants.ts";
import { countdown } from "./deadline.ts";
import { TaskCard } from "./task.tsx";
import type { DashboardResponse, Item } from "./types.ts";

function statusCounts(items: Item[]): string {
  const counts: Record<string, number> = {};
  for (const item of items) counts[item.status] = (counts[item.status] ?? 0) + 1;
  return Object.keys(counts)
    .sort()
    .map((status) => `${status}: ${counts[status]}`)
    .join(" · ");
}

interface InboxProps {
  data: DashboardResponse | null;
  error: string | null;
  clicked: ReadonlySet<string>;
  onToggle: (itemId: string, actionId: string) => void;
  actionError: string | null;
  now: number;
}

// The triage board: a value-ranked list of items with a time-critical "Due soon" section.
// One surface among several (see App's tabs) — not the whole of haku-ui.
export function InboxView({ data, error, clicked, onToggle, actionError, now }: InboxProps) {
  if (error)
    return (
      <Text c="red" my="lg">
        Failed to load: {error}
      </Text>
    );
  if (!data)
    return (
      <Text c="dimmed" my="lg">
        Loading…
      </Text>
    );

  const open = data.items.filter((item) => item.status === "open");
  // Time-critical first: anything overdue or within DUE_SOON_DAYS, soonest deadline on top.
  const dueSoon = open
    .filter((item) => item.deadline && countdown(item.deadline, now).urgency !== "later")
    .sort((a, b) => new Date(a.deadline!).getTime() - new Date(b.deadline!).getTime());
  const dueSoonIds = new Set(dueSoon.map((item) => item.id));
  // Everything else stays value-ranked; due-soon items are pulled out so they appear once.
  const ranked = open.filter((item) => !dueSoonIds.has(item.id)).sort((a, b) => b.value - a.value);
  const upNext = ranked.slice(0, UP_NEXT);
  const backlog = ranked.slice(UP_NEXT);

  return (
    <Stack gap="md">
      {actionError && <Text c="red">Action failed: {actionError}</Text>}

      {dueSoon.length > 0 && (
        <div>
          <Title order={2} c="orange" size="h4" mb="xs">
            ⏰ Due soon
          </Title>
          {dueSoon.map((item) => (
            <TaskCard key={item.id} item={item} clicked={clicked} onToggle={onToggle} now={now} />
          ))}
        </div>
      )}

      <div>
        <Title order={2} size="h4" mb="xs">
          Up next
        </Title>
        {upNext.length > 0 ? (
          upNext.map((item) => <TaskCard key={item.id} item={item} clicked={clicked} onToggle={onToggle} now={now} />)
        ) : (
          <Text>No open items.</Text>
        )}
      </div>

      {backlog.length > 0 && (
        <Accordion variant="separated">
          <Accordion.Item value="backlog">
            <Accordion.Control>Backlog — {backlog.length} more open item(s)</Accordion.Control>
            <Accordion.Panel>
              {backlog.map((item) => (
                <TaskCard key={item.id} item={item} clicked={clicked} onToggle={onToggle} now={now} />
              ))}
            </Accordion.Panel>
          </Accordion.Item>
        </Accordion>
      )}

      <Text size="sm" c="dimmed" mt="xl" pt="md" style={{ borderTop: "1px solid var(--mantine-color-default-border)" }}>
        {open.length} open · {statusCounts(data.items)}
        <br />
        Last scan:{" "}
        <time dateTime={data.scan_time} title={data.scan_time}>
          {new Date(data.scan_time).toLocaleString()}
        </time>
        {data.deployed_commit && data.deployed_commit_url && (
          <>
            {" · deployed "}
            <Anchor inherit onClick={() => void openLink(data.deployed_commit_url!)} style={{ cursor: "pointer" }}>
              <Code>{data.deployed_commit}</Code>
            </Anchor>
          </>
        )}
      </Text>
    </Stack>
  );
}
