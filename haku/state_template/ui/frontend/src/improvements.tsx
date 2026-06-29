import { Badge, Card, Group, Spoiler, Stack, Text, Title } from "@mantine/core";
import { useEffect, useState } from "react";

import { fetchImprovements } from "./client.ts";
import { renderMarkdown } from "./markdown.ts";
import type { Friction, ImprovementIdea, ImprovementsBoard } from "./types.ts";

// Haku's read-only self-backlog: capability ideas it could grow into, and friction it hits
// during runs (data-access gaps, flaky/limited backends) that the operator might want to fix.
// Source: improvements.yaml, gardened each run (procedures/maintenance_and_synthesis.md).

const VALUE_RANK = { high: 0, medium: 1, low: 2 } as const;
// Open problems first; the rest are FYI / closed-loop.
const FRICTION_RANK = { open: 0, workaround: 1, answered: 2, resolved: 3 } as const;

// value/severity share one high→red, medium→orange, low→gray scale.
const VALUE_COLOR: Record<ImprovementIdea["value"], string> = { high: "red", medium: "orange", low: "gray" };
// recommend/open are the "act on this" statuses (blue, filled); everything else is FYI (gray, light).
const ACTIVE_STATUSES = new Set(["recommend", "open"]);

function statusBadge(status: string) {
  return ACTIVE_STATUSES.has(status) ? (
    <Badge size="sm" variant="filled" color="blue">
      {status}
    </Badge>
  ) : (
    <Badge size="sm" variant="light" color="gray">
      {status}
    </Badge>
  );
}

function ideaCard(idea: ImprovementIdea) {
  return (
    <Card key={idea.id} withBorder padding="sm" radius="md">
      <Group gap="xs" wrap="wrap" align="baseline">
        <Text fw={600} style={{ flex: 1, minWidth: 0 }}>
          {idea.title}
        </Text>
        <Badge size="sm" variant="light" color={VALUE_COLOR[idea.value]}>
          {idea.value} value
        </Badge>
        {statusBadge(idea.status)}
      </Group>
      <Text size="sm" c="dimmed" mt="xs">
        {idea.summary}
      </Text>
      {idea.detail ? (
        <Spoiler maxHeight={0} showLabel="details" hideLabel="hide" mt="xs">
          <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(idea.detail) }} />
        </Spoiler>
      ) : null}
    </Card>
  );
}

function frictionCard(f: Friction) {
  return (
    <Card key={f.id} withBorder padding="sm" radius="md">
      <Group gap="xs" wrap="wrap" align="baseline">
        <Text fw={600} style={{ flex: 1, minWidth: 0 }}>
          {f.title}
        </Text>
        <Badge size="sm" variant="light" color={VALUE_COLOR[f.severity]}>
          {f.severity}
        </Badge>
        {statusBadge(f.status)}
      </Group>
      {f.detail ? <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(f.detail) }} /> : null}
    </Card>
  );
}

export function ImprovementsPage() {
  const [data, setData] = useState<ImprovementsBoard | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchImprovements()
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  if (error) return <Text c="red">Failed to load improvements: {error}</Text>;
  if (!data) return <Text c="dimmed">Loading…</Text>;

  const ideas = [...data.ideas].sort((a, b) => VALUE_RANK[a.value] - VALUE_RANK[b.value]);
  const friction = [...data.friction].sort((a, b) => FRICTION_RANK[a.status] - FRICTION_RANK[b.status]);
  const openCount = friction.filter((f) => f.status === "open").length;

  return (
    <Stack gap="lg">
      <Text size="sm" c="dimmed">
        What would make me more useful, and what's getting in my way — Haku's own backlog, value-ranked. Steer it with a
        note from the Inbox tab. {data.updated ? `Updated ${new Date(data.updated).toLocaleString()}.` : null}
      </Text>

      <Stack gap="sm">
        <Title order={3} size="h5">
          💡 Capability ideas ({ideas.length})
        </Title>
        {ideas.map(ideaCard)}
      </Stack>

      <Stack gap="sm">
        <Title order={3} size="h5">
          🔧 Friction &amp; breakages ({friction.length}
          {openCount > 0 ? `, ${openCount} open` : ""})
        </Title>
        {friction.map(frictionCard)}
      </Stack>
    </Stack>
  );
}
