import { Badge, Card, Group, Stack, Text, Title } from "@mantine/core";
import { useEffect, useState } from "react";

import { Disclosure } from "./disclosure.tsx";
import { renderMarkdown } from "./markdown.ts";
import { type Doc, docsUnder } from "./repo.ts";

// The `<improvement-board>` garden widget: renders Haku's self-backlog as a live view over the
// markdown files under `memory/improvements/` (one .md per entry, `kind: improvement` frontmatter),
// queried through the generic tree+blobs proxy. Same data, one presentation — a garden doc embeds
// `<improvement-board></improvement-board>` and this fetches + renders it. Replaces the bespoke
// ImprovementsPage/improvements.yaml pipeline. See plans/garden-gradient.md → Settled mechanism.

type Weight = "high" | "medium" | "low";

interface ImprovementDoc {
  path: string;
  cls: "idea" | "friction"; // which board section
  title: string;
  weight: Weight; // idea → value, friction → severity; one high→red/medium→orange/low→gray scale
  status: string;
  summary: string; // ideas carry a one-liner; friction usually omits it
  body: string; // markdown detail
}

const WEIGHTS = new Set<string>(["high", "medium", "low"]);
const WEIGHT_COLOR: Record<Weight, string> = { high: "red", medium: "orange", low: "gray" };
const WEIGHT_RANK: Record<Weight, number> = { high: 0, medium: 1, low: 2 };
// recommend/open are the "act on this" statuses; everything else is FYI/closed-loop.
const ACTIVE = new Set(["recommend", "open"]);

const str = (v: unknown): string => (typeof v === "string" ? v : "");

// Defensive frontmatter → typed doc: only `kind: improvement` files, with fallbacks for any
// missing/invalid field so one malformed file can never crash the board (never throws).
function toDoc(e: Doc): ImprovementDoc | null {
  const d = e.data;
  if (d.kind !== "improvement") return null;
  const weight = str(d.weight);
  return {
    path: e.path,
    cls: d.class === "friction" ? "friction" : "idea",
    title: str(d.title) || e.path,
    weight: (WEIGHTS.has(weight) ? weight : "medium") as Weight,
    status: str(d.status),
    summary: str(d.summary),
    body: e.body.trim(),
  };
}

// Actionable first (recommend/open), then by weight high→low.
const rank = (d: ImprovementDoc): number => (ACTIVE.has(d.status) ? 0 : 10) + WEIGHT_RANK[d.weight];

function statusBadge(status: string) {
  return ACTIVE.has(status) ? (
    <Badge size="sm" variant="filled" color="blue">
      {status}
    </Badge>
  ) : (
    <Badge size="sm" variant="light" color="gray">
      {status}
    </Badge>
  );
}

function card(d: ImprovementDoc) {
  return (
    <Card key={d.path} withBorder padding="sm" radius="md">
      <Group gap="xs" wrap="wrap" align="baseline">
        <Text fw={600} style={{ flex: 1, minWidth: 0 }}>
          {d.title}
        </Text>
        <Badge size="sm" variant="light" color={WEIGHT_COLOR[d.weight]}>
          {d.cls === "idea" ? `${d.weight} value` : d.weight}
        </Badge>
        {statusBadge(d.status)}
      </Group>
      {d.summary ? (
        <Text size="sm" c="dimmed" mt="xs">
          {d.summary}
        </Text>
      ) : null}
      {d.body ? (
        <Stack gap={4} mt="xs">
          <Disclosure
            header={
              <Text size="sm" c="dimmed">
                details
              </Text>
            }
          >
            <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(d.body) }} />
          </Disclosure>
        </Stack>
      ) : null}
    </Card>
  );
}

export function ImprovementBoard() {
  const [docs, setDocs] = useState<ImprovementDoc[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    docsUnder("memory/improvements")
      .then((entries) => setDocs(entries.map(toDoc).filter((d): d is ImprovementDoc => d !== null)))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  if (error) return <Text c="red">Failed to load improvements: {error}</Text>;
  if (!docs) return <Text c="dimmed">Loading…</Text>;

  const ideas = docs.filter((d) => d.cls === "idea").sort((a, b) => rank(a) - rank(b));
  const friction = docs.filter((d) => d.cls === "friction").sort((a, b) => rank(a) - rank(b));
  const openCount = friction.filter((d) => ACTIVE.has(d.status)).length;

  return (
    <Stack gap="lg">
      <Stack gap="sm">
        <Title order={3} size="h5">
          💡 Capability ideas ({ideas.length})
        </Title>
        {ideas.map(card)}
      </Stack>
      <Stack gap="sm">
        <Title order={3} size="h5">
          🔧 Friction &amp; breakages ({friction.length}
          {openCount > 0 ? `, ${openCount} open` : ""})
        </Title>
        {friction.map(card)}
      </Stack>
    </Stack>
  );
}
