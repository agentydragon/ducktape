import { Anchor, Group, Stack, Table, Text, Title } from "@mantine/core";
import { useEffect, useState } from "react";

import { fetchRuns } from "./client.ts";
import { errText } from "./errors.ts";
import { LoadError } from "./load_error.tsx";
import { Mdx } from "./mdx.tsx";
import type { RunManifest, RunSource, ScannedSource } from "./types.ts";
import { PropagationMatrix } from "./widgets.tsx";

function whenLabel(run: RunManifest): string {
  return run.started ? new Date(run.started).toLocaleString() : run.date;
}

// created/updated both land content on a surface; no_change/n/a don't. This is the "did the run
// actually move anything" count the operator scans the list for.
function surfaceUpdateCount(run: RunManifest): number {
  return run.propagation.reduce(
    (n, p) => n + p.surfaces.filter((s) => s.action === "updated" || s.action === "created").length,
    0
  );
}

function scannedCount(sources: RunSource[]): number {
  return sources.filter((s) => !("skipped" in s)).length;
}

function skippedCount(sources: RunSource[]): number {
  return sources.filter((s) => "skipped" in s).length;
}

// A run's changes_seen is a real count when countable, else a short prose summary — render either
// as-is (0 stays dimmed so "scanned, nothing new" reads quiet, not like a signal).
function ChangesCell({ value }: { value: number | string }) {
  const empty = value === 0 || value === "0";
  return (
    <Text size="sm" c={empty ? "dimmed" : undefined}>
      {value}
    </Text>
  );
}

function bookmarkLabel(s: ScannedSource): string | null {
  if (s.bookmark_before == null && s.bookmark_after == null) return null;
  return `${s.bookmark_before ?? "—"} → ${s.bookmark_after ?? "—"}`;
}

// The runs list as a scannable table — one row per run, no per-source color wall. Numbers on the
// right, coverage folded into one "N scanned · M skipped" cell (skipped in a subtle warning color,
// the only color that carries signal here). The whole row opens the detail (click or Enter/Space).
function RunsTable({ runs, onOpen }: { runs: RunManifest[]; onOpen: (runId: string) => void }) {
  return (
    <Table.ScrollContainer minWidth={480}>
      <Table highlightOnHover verticalSpacing="sm" horizontalSpacing="md">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>When</Table.Th>
            <Table.Th>Sources</Table.Th>
            <Table.Th ta="right">Changes</Table.Th>
            <Table.Th ta="right">Surface updates</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {runs.map((run) => {
            const skipped = skippedCount(run.sources);
            return (
              <Table.Tr
                key={run.run_id}
                role="button"
                tabIndex={0}
                onClick={() => onOpen(run.run_id)}
                onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onOpen(run.run_id))}
                style={{ cursor: "pointer" }}
              >
                <Table.Td>{whenLabel(run)}</Table.Td>
                <Table.Td>
                  <Text span size="sm">
                    {scannedCount(run.sources)} scanned
                  </Text>
                  {skipped > 0 && (
                    <Text span size="sm" c="orange">
                      {" · "}
                      {skipped} skipped
                    </Text>
                  )}
                </Table.Td>
                <Table.Td ta="right">{run.propagation.length}</Table.Td>
                <Table.Td ta="right">{surfaceUpdateCount(run)}</Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  );
}

// Source coverage for one run, as a table (Source · Changes · Bookmark before→after). A skipped
// source spans the trailing columns with its reason in a subtle warning color, so a coverage gap
// reads as one calm line, not a color wall.
function SourceTable({ sources }: { sources: RunSource[] }) {
  if (sources.length === 0) return null;
  return (
    <Table withTableBorder fz="sm" verticalSpacing="xs" horizontalSpacing="md">
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Source</Table.Th>
          <Table.Th>Changes</Table.Th>
          <Table.Th>Bookmark</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {sources.map((s) =>
          "skipped" in s ? (
            <Table.Tr key={s.source}>
              <Table.Td>{s.source}</Table.Td>
              <Table.Td colSpan={2}>
                <Text size="sm" c="orange">
                  Skipped — {s.skipped}
                </Text>
              </Table.Td>
            </Table.Tr>
          ) : (
            <Table.Tr key={s.source}>
              <Table.Td>{s.source}</Table.Td>
              <Table.Td>
                <ChangesCell value={s.changes_seen} />
              </Table.Td>
              <Table.Td>
                {bookmarkLabel(s) && (
                  <Text size="sm" c="dimmed" ff="monospace">
                    {bookmarkLabel(s)}
                  </Text>
                )}
              </Table.Td>
            </Table.Tr>
          )
        )}
      </Table.Tbody>
    </Table>
  );
}

// The full per-run detail: source coverage table, the checklists walked, the change→surface
// propagation matrix (the shared widget), and the prose notes rendered as MDX.
function RunDetail({
  run,
  onBack,
  openInGarden,
}: {
  run: RunManifest;
  onBack: () => void;
  openInGarden: (path: string) => void;
}) {
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

      <Stack gap="xs">
        <Title order={3} size="h6">
          Sources
        </Title>
        <SourceTable sources={run.sources} />
      </Stack>

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
          <PropagationMatrix data={run.propagation} onNavigate={openInGarden} />
        </Stack>
      )}

      {run.notes_md && (
        <Mdx source={run.notes_md} basePath={`runs/${run.date}/${run.run_id}.md`} onNavigate={openInGarden} />
      )}
    </Stack>
  );
}

// The Runs surface: a scannable table of runs → click into the full per-run propagation detail.
// Proves every source was processed and shows how each change reached its surfaces. Read-only;
// backed by runs/<date>/<ulid>.{yaml,md}.
export function RunsPage({ openInGarden }: { openInGarden: (path: string) => void }) {
  const [runs, setRuns] = useState<RunManifest[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    fetchRuns()
      .then((r) => setRuns(r.runs))
      .catch((e: unknown) => setError(errText(e)));
  }, []);

  if (error) return <LoadError what="runs" error={error} />;
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
  if (open) return <RunDetail run={open} onBack={() => setSelected(null)} openInGarden={openInGarden} />;

  return (
    <Stack gap="md">
      <Text size="sm" c="dimmed">
        Each run: every source I processed, and how each change propagated to your surfaces. Click a run for detail.
      </Text>
      <RunsTable runs={runs} onOpen={setSelected} />
    </Stack>
  );
}
