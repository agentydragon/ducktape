import { Container, Tabs, Title } from "@mantine/core";
import { useEffect, useState } from "react";

import { clickAction, fetchDashboard, unclickAction } from "./client.ts";
import { ImprovementsPage } from "./improvements.tsx";
import { InboxView } from "./inbox.tsx";
import { RunsPage } from "./runs.tsx";
import { clickKey } from "./task.tsx";
import type { DashboardResponse } from "./types.ts";

// haku-ui is a multi-surface app Haku owns and evolves — NOT a fixed dashboard. The starter
// ships two person-agnostic surfaces: the **Inbox** (the items board) and **Improvements**
// (Haku's self-backlog). Haku adds more tabs as bespoke surfaces for its operator's life
// (e.g. a Kitchen/shopping board, a one-off decision page) by writing a new `*.tsx`, a
// backend endpoint, and a `View` entry here. Those operator-specific surfaces live in that
// operator's haku-state, not in this generic starter.
type View = "inbox" | "improvements" | "runs";

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
    ["runs", "🔁 Runs"],
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
      ) : view === "runs" ? (
        <RunsPage />
      ) : (
        <InboxView
          data={data}
          error={error}
          clicked={clicked}
          onToggle={onToggle}
          actionError={actionError}
          now={now}
        />
      )}
    </Container>
  );
}
