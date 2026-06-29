import { Accordion, Anchor, Code, Container, Stack, Tabs, Text, Title } from "@mantine/core";
import { useEffect, useState } from "react";

import { openLink } from "./bridge.ts";
import { clickAction, fetchDashboard, unclickAction } from "./client.ts";
import { UP_NEXT } from "./constants.ts";
import { countdown } from "./deadline.ts";
import { ImprovementsPage } from "./improvements.tsx";
import { TaskCard, clickKey } from "./task.tsx";
import type { DashboardResponse, Item } from "./types.ts";

// haku-ui is a multi-surface app Haku owns and evolves — NOT a fixed dashboard. The starter
// ships two person-agnostic surfaces: the **Inbox** (the items board) and **Improvements**
// (Haku's self-backlog). Haku adds more tabs as bespoke surfaces for its operator's life
// (e.g. a Kitchen/shopping board, a one-off decision page) by writing a new `*.tsx`, a
// backend endpoint, and a `View` entry here. Those operator-specific surfaces live in that
// operator's haku-state, not in this generic starter.
type View = "inbox" | "improvements";

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
function InboxView({ data, error, clicked, onToggle, actionError, now }: InboxProps) {
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

export default function App() {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [clicked, setClicked] = useState<ReadonlySet<string>>(new Set());
  const [actionError, setActionError] = useState<string | null>(null);
  const [view, setView] = useState<View>("inbox");
  // Ticks the live deadline countdowns; 30s is fine at minute granularity.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    let alive = true;
    fetchDashboard()
      .then((dashboard) => {
        if (!alive) return;
        setData(dashboard);
        setClicked(new Set(dashboard.clicks.map((c) => clickKey(c.item_id, c.action_id))));
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  function onToggle(itemId: string, actionId: string) {
    const key = clickKey(itemId, actionId);
    const wasClicked = clicked.has(key);
    const next = new Set(clicked);
    if (wasClicked) next.delete(key);
    else next.add(key);
    setClicked(next); // optimistic; reverted below on failure
    setActionError(null);
    void (wasClicked ? unclickAction(itemId, actionId) : clickAction(itemId, actionId)).catch((e: unknown) => {
      const reverted = new Set(next);
      if (wasClicked) reverted.add(key);
      else reverted.delete(key);
      setClicked(reverted);
      setActionError(e instanceof Error ? e.message : String(e));
    });
  }

  const tabs: [View, string][] = [
    ["inbox", "Inbox"],
    ["improvements", "💡 Improvements"],
  ];

  return (
    <Container size={760} px="md" py="xl">
      <Title order={1} mb="sm">
        Haku
      </Title>
      <Tabs value={view} onChange={(value) => value && setView(value as View)} mb="md">
        <Tabs.List>
          {tabs.map(([id, label]) => (
            <Tabs.Tab key={id} value={id}>
              {label}
            </Tabs.Tab>
          ))}
        </Tabs.List>
      </Tabs>

      {view === "improvements" ? (
        <ImprovementsPage />
      ) : (
        <InboxView data={data} error={error} clicked={clicked} onToggle={onToggle} actionError={actionError} now={now} />
      )}
    </Container>
  );
}
