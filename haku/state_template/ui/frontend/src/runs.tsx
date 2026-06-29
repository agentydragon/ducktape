import { Anchor, Badge, Card, Group, Stack, Text, Title } from "@mantine/core";
import { useEffect, useState } from "react";

import { fetchRuns } from "./client.ts";
import { renderMarkdown } from "./markdown.ts";
import type { PropagationTarget, RunManifest, RunSource } from "./types.ts";

// updated = it landed; no_change = considered, didn't apply; n/a = surface never applies.
const ACTION_COLOR: Record<PropagationTarget["action"], string> = {
  updated: "teal",
  no_change: "gray",
  "n/a": "gray",
};

// One badge per source: how many changes it produced, or that it was skipped (with the reason
// on hover) — so "considered every source" is legible, and a skipped source is loud, not silent.
function sourceBadge(s: RunSource) {
  if (s.skipped != null) {
    return (
      <Badge key={s.source} color="yellow" variant="light" title={s.skipped}>
        {s.source}: skipped
      </Badge>
    );
  }
  const n = s.changes_seen ?? 0;
  return (
    <Badge key={s.source} color={n > 0 ? "teal" : "gray"} variant="light">
      {s.source}: {n}
    </Badge>
  );
}

function whenLabel(run: RunManifest): string {
  return run.started ? new Date(run.started).toLocaleString() : run.date;
}

// A concise, clickable row in the runs list: when, the source-coverage badges, and a one-line
// summary (changes / surface updates / skipped sources). Click → the full per-run detail.
function RunRow({ run, onOpen }: { run: RunManifest; onOpen: () => void }) {
  const updated = run.propagation.reduce((n, p) => n + p.surfaces.filter((s) => s.action === "updated").length, 0);
  const skipped = run.sources.filter((s) => s.skipped != null).length;
  return (
    <Card
      withBorder
      padding="sm"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onOpen())}
      style={{ cursor: "pointer" }}
    >
      <Group justify="space-between" align="center" wrap="nowrap">
        <Text fw={600}>Run · {whenLabel(run)}</Text>
        <Text size="xs" c="dimmed">
          {run.propagation.length} change{run.propagation.length === 1 ? "" : "s"} · {updated} surface update
          {updated === 1 ? "" : "s"}
          {skipped ? ` · ${skipped} skipped` : ""}
        </Text>
      </Group>
      {run.sources.length > 0 && (
        <Group gap="xs" mt="xs">
          {run.sources.map(sourceBadge)}
        </Group>
      )}
    </Card>
  );
}

// The full per-run detail: source coverage, the checklists walked, the change→surface propagation
// matrix, and the prose notes rendered as markdown. (Phase 2 upgrades the notes render to the MDX
// knowledge-garden renderer so a run can embed widgets like a propagation matrix.)
function RunDetail({ run, onBack }: { run: RunManifest; onBack: () => void }) {
  return (
    <Stack gap="md">
      <Group justify="space-between" align="center">
        <Anchor onClick={onBack} style={{ cursor: "pointer" }}>
          ← All runs
        </Anchor>
        <Text size="xs" c="dimmed">
          {run.run_id}
        </Text>
      </Group>
      <Title order={2} size="h4">
        Run · {whenLabel(run)}
      </Title>

      {run.sources.length > 0 && <Group gap="xs">{run.sources.map(sourceBadge)}</Group>}

      {run.checklists.length > 0 && (
        <Text size="sm" c="dimmed">
          Checklists: {run.checklists.map((c) => `${c.checklist} ${c.walked ? "✓" : "✗"}`).join(" · ")}
        </Text>
      )}

      {run.propagation.length > 0 && (
        <Stack gap="xs">
          <Title order={3} size="h6">
            Propagation
          </Title>
          {run.propagation.map((p, i) => (
            <div key={i}>
              <Text size="sm" fw={600}>
                {p.change}
                {p.source && (
                  <Text span size="xs" c="dimmed">
                    {" "}
                    ({p.source})
                  </Text>
                )}
              </Text>
              <Group gap="xs" mt={4}>
                {p.surfaces.map((t, j) => (
                  <Badge key={j} color={ACTION_COLOR[t.action]} variant="dot" title={t.note}>
                    {t.surface}: {t.action}
                  </Badge>
                ))}
              </Group>
            </div>
          ))}
        </Stack>
      )}

      {run.notes_md && <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(run.notes_md) }} />}
    </Stack>
  );
}

// The Runs surface: a list of runs (concise structured summary) → click into the full per-run
// propagation detail. Proves every source was processed and shows how each change reached its
// surfaces. Read-only; backed by runs/<date>/<ulid>.{yaml,md}.
export function RunsPage() {
  const [runs, setRuns] = useState<RunManifest[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    fetchRuns()
      .then((r) => setRuns(r.runs))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  if (error)
    return (
      <Text c="red" my="lg">
        Failed to load runs: {error}
      </Text>
    );
  if (!runs)
    return (
      <Text c="dimmed" my="lg">
        Loading…
      </Text>
    );
  if (runs.length === 0)
    return (
      <Text c="dimmed" my="lg">
        No runs recorded yet.
      </Text>
    );

  const open = selected ? runs.find((r) => r.run_id === selected) : null;
  if (open) return <RunDetail run={open} onBack={() => setSelected(null)} />;

  return (
    <Stack gap="md">
      <Text size="sm" c="dimmed">
        Each run: every source I processed, and how each change propagated to your surfaces. Click a run for detail.
      </Text>
      {runs.map((run) => (
        <RunRow key={run.run_id} run={run} onOpen={() => setSelected(run.run_id)} />
      ))}
    </Stack>
  );
}
