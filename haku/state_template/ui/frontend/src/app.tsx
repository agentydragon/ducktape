import { Box, Container, Group, Tabs, Title } from "@mantine/core";
import { useEffect, useState } from "react";

import { notifyRouteChanged } from "./bridge.ts";
import { fetchMeta } from "./client.ts";
import { NoteToHaku } from "./feedback_button.tsx";
import { GitProgressBar } from "./git_progress_bar.tsx";
import { GardenPage } from "./garden.tsx";
import { ImprovementBoard } from "./improvement_board.tsx";
import { InboxView } from "./inbox.tsx";
import { LaunchButton } from "./launch.tsx";
import { type Doc, docsUnder } from "./repo.ts";
import { formatHash, useHashRoute } from "./routes.ts";
import type { View } from "./routes.ts";
import { RunsPage } from "./runs.tsx";
import type { MetaResponse } from "./types.ts";

export default function App() {
  // Items are read straight from `items/*.md`; `meta` is just the footer's freshness/deploy stamp.
  const [docItems, setDocItems] = useState<Doc[] | null>(null);
  const [meta, setMeta] = useState<MetaResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The URL hash is the source of truth for which surface (and which garden file) is open —
  // F5, permalinks, and back/forward come from the browser (routes.ts → useHashRoute).
  const [route, navigate] = useHashRoute();
  const { view, gardenPath } = route;
  // Mirror every route change (and the initial route) to the console shell so its URL
  // fragment tracks this view — the shell restores it into the iframe src on reload.
  useEffect(() => {
    notifyRouteChanged(formatHash(route).slice(1));
  }, [route]);
  function openInGarden(path: string) {
    navigate({ view: "garden", gardenPath: path });
  }
  // Ticks the live deadline countdowns; 30s is fine at minute granularity.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    let alive = true;
    docsUnder("items")
      .then((docs) => alive && setDocItems(docs))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)));
    // Footer metadata is best-effort — the board still renders without it.
    fetchMeta()
      .then((m) => alive && setMeta(m))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const tabs: [View, string][] = [
    ["inbox", "Inbox"],
    ["improvements", "💡 Improvements"],
    ["runs", "🔁 Runs"],
    ["garden", "🌱 Garden"],
  ];

  return (
    <Container size={760} px="md" pb="xl">
      {/* App-wide git transfer indicator: fixed to the viewport top, shows whenever any repo
          tree/blob read is in flight (any surface), idle otherwise. */}
      <GitProgressBar />
      {/* The header (logo + Note/Launch) and the tab strip stay pinned as content scrolls
            (operator: keep the top menu fixed). Sticky within the scrolling iframe body; a solid
            body background + subtle shadow so content scrolls under it and it reads as a bar. */}
      <Box
        pt="xl"
        style={{
          position: "sticky",
          top: 0,
          zIndex: 100,
          background: "var(--mantine-color-body)",
          boxShadow: "var(--mantine-shadow-sm)",
        }}
      >
        <Group justify="space-between" align="center" mb="sm">
          <Group gap="sm" align="center">
            <img src="/logo.svg" alt="" aria-hidden="true" width={36} height={36} style={{ flexShrink: 0 }} />
            <Title order={1}>Haku</Title>
          </Group>
          <Group gap="sm">
            <NoteToHaku />
            <LaunchButton />
          </Group>
        </Group>
        <Tabs value={view} onChange={(value) => value && navigate({ view: value as View, gardenPath: null })}>
          <Tabs.List>
            {tabs.map(([id, label]) => (
              <Tabs.Tab key={id} value={id}>
                {label}
              </Tabs.Tab>
            ))}
          </Tabs.List>
        </Tabs>
      </Box>

      <Box mt="md">
        {view === "improvements" ? (
          <ImprovementBoard />
        ) : view === "runs" ? (
          <RunsPage openInGarden={openInGarden} />
        ) : view === "garden" ? (
          <GardenPage path={gardenPath} onSelect={(path) => navigate({ view: "garden", gardenPath: path })} />
        ) : (
          <InboxView docItems={docItems} meta={meta} error={error} now={now} onNavigate={openInGarden} />
        )}
      </Box>
    </Container>
  );
}
