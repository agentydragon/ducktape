import { Anchor, Group, NavLink, Stack, Text, Title } from "@mantine/core";
import { useEffect, useState } from "react";

import { Mdx } from "./mdx.tsx";
import { repoFile, repoTree } from "./repo.ts";

// Curated browse dirs for the garden index. The raw content proxy is repo-wide (haku-state is
// single-author, no secrets); this is purely which notes show up in the browse list — curation,
// not a security fence. (Was the backend GARDEN_DIRS; moved here when the garden read collapsed
// onto the generic tree+blobs proxy — see plans/garden-gradient.md.)
const GARDEN_DIRS = ["memory", "procedures", "runs"];

// The top-level dir (memory/procedures/runs) a garden path lives under — used to group the list.
function topDir(path: string): string {
  return path.split("/")[0];
}

// The rendered pane: fetch one file's markdown (via the content proxy) and render it as MDX (so
// embedded widgets work, and internal links navigate the garden via onNavigate).
function FileView({ path, onNavigate }: { path: string; onNavigate: (path: string) => void }) {
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setMarkdown(null);
    setError(null);
    repoFile(path)
      .then((md) => alive && (md === null ? setError("not found") : setMarkdown(md)))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [path]);

  if (error)
    return (
      <Text c="red">
        Failed to load {path}: {error}
      </Text>
    );
  if (markdown === null) return <Text c="dimmed">Loading…</Text>;
  return <Mdx source={markdown} basePath={path} onNavigate={onNavigate} />;
}

// The knowledge garden: browse any markdown Haku keeps under the curated dirs (memory/,
// procedures/, runs/) and read it rendered (with standard widgets + working cross-links). A general
// primitive — the same renderer backs run notes. Read-only. `path`/`onSelect` are controlled so
// other surfaces (a run note, an item) can deep-link straight into a file.
export function GardenPage({ path, onSelect }: { path: string | null; onSelect: (path: string | null) => void }) {
  const [paths, setPaths] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    repoTree()
      .then((t) =>
        setPaths(
          t.entries
            .filter((e) => e.type === "blob" && /\.mdx?$/.test(e.path) && GARDEN_DIRS.includes(topDir(e.path)))
            .map((e) => e.path)
            .sort()
        )
      )
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  // A selected file renders independently of the index (so a deep-link works before it loads).
  if (path)
    return (
      <Stack gap="md">
        <Group justify="space-between" align="center">
          <Anchor onClick={() => onSelect(null)} style={{ cursor: "pointer" }}>
            ← All notes
          </Anchor>
          <Text size="xs" c="dimmed">
            {path}
          </Text>
        </Group>
        <FileView path={path} onNavigate={onSelect} />
      </Stack>
    );

  if (error) return <Text c="red">Failed to load garden: {error}</Text>;
  if (!paths) return <Text c="dimmed">Loading…</Text>;
  if (paths.length === 0) return <Text c="dimmed">The garden is empty.</Text>;

  // Group by top dir for a scannable list; within a group, paths are already sorted.
  const groups = [...new Set(paths.map(topDir))];

  return (
    <Stack gap="lg">
      <Text size="sm" c="dimmed">
        My notebook — memory, procedures, and run notes, interlinked and rendered with standard widgets. Read-only.
      </Text>
      {groups.map((dir) => (
        <Stack gap={4} key={dir}>
          <Title order={3} size="h6">
            {dir}/
          </Title>
          {paths
            .filter((p) => topDir(p) === dir)
            .map((p) => (
              <NavLink
                key={p}
                label={p.slice(dir.length + 1)}
                onClick={() => onSelect(p)}
                style={{ cursor: "pointer" }}
              />
            ))}
        </Stack>
      ))}
    </Stack>
  );
}
