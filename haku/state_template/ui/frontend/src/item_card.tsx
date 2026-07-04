import { Badge, Card, Collapse, Group, Stack, Text, UnstyledButton } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { useEffect, useState } from "react";

import { countdown, type Urgency } from "./deadline.ts";
import { errText } from "./errors.ts";
import { parseFrontmatter } from "./frontmatter.ts";
import { ItemScopeContext } from "./item_scope.ts";
import { Mdx } from "./mdx.tsx";
import { repoFile } from "./repo.ts";

const URGENCY_COLOR: Record<Urgency, string> = { overdue: "red", soon: "orange", later: "gray" };

// One new-format item rendered from its markdown: the header (value/title/deadline/status) comes
// from frontmatter; the body renders via <Mdx> so its embedded affordances (<handoff>,
// <signal-toggle>) come alive. The item's slug is provided on ItemScopeContext, so an item-scoped
// `<signal-toggle field="status">` in the body resolves its `scope` automatically. The Inbox calls
// this directly with already-loaded data; `<item-card id=…>` (below) is the by-reference embed.
export function ItemCard({
  slug,
  title,
  value,
  status,
  deadline,
  body,
  now,
  onNavigate,
}: {
  slug: string;
  title: string;
  value: number;
  status: string;
  deadline?: string | null;
  body: string;
  now: number;
  onNavigate?: (path: string) => void;
}) {
  const [opened, { toggle }] = useDisclosure(false);
  const cd = deadline ? countdown(deadline, now) : null;
  return (
    <ItemScopeContext.Provider value={slug}>
      <Card withBorder radius="md" padding="sm" mb="xs">
        {/* Collapsed by default — the header (value/title/badges) toggles the body, which holds the
            prose + its affordances. Same shape as the legacy TaskCard so the board reads uniformly. */}
        <UnstyledButton onClick={toggle} aria-expanded={opened} style={{ width: "100%" }}>
          <Stack gap={6}>
            <Group gap="xs" wrap="nowrap" align="baseline" style={{ width: "100%" }}>
              <Text c="dimmed" size="sm" aria-hidden style={{ flexShrink: 0 }}>
                {opened ? "▾" : "▸"}
              </Text>
              <Text fw={700} c="teal" size="lg" fz="1.125rem" style={{ flexShrink: 0 }}>
                {value}
              </Text>
              <Text fw={600} style={{ flex: 1, minWidth: 0 }}>
                {title}
              </Text>
            </Group>
            {(cd || status !== "open") && (
              <Group gap="xs" wrap="wrap" pl={28}>
                {cd && (
                  <Badge
                    color={URGENCY_COLOR[cd.urgency]}
                    variant="filled"
                    size="sm"
                    style={{ fontVariantNumeric: "tabular-nums" }}
                  >
                    ⏳ {cd.text}
                  </Badge>
                )}
                {status !== "open" && (
                  <Badge color="gray" variant="light" size="sm">
                    {status}
                  </Badge>
                )}
              </Group>
            )}
          </Stack>
        </UnstyledButton>
        <Collapse expanded={opened}>
          <div className="md" style={{ marginTop: 8 }}>
            <Mdx source={body} basePath={`items/${slug}.md`} onNavigate={onNavigate} />
          </div>
        </Collapse>
      </Card>
    </ItemScopeContext.Provider>
  );
}

type LoadState = "loading" | { error: string } | { data: Record<string, unknown>; body: string };

const asStr = (v: unknown): string => (typeof v === "string" ? v : "");
const asNum = (v: unknown): number => (typeof v === "number" ? v : 0);

// `<item-card id="slug">` — fetch one item by slug and render it inline (e.g. referenced from a
// garden note): a stronger "link, don't lecture" — the note shows the item's live card, not a link.
export function ItemCardById({
  id,
  now,
  onNavigate,
}: {
  id: string;
  now: number;
  onNavigate?: (path: string) => void;
}) {
  const [state, setState] = useState<LoadState>("loading");
  useEffect(() => {
    let live = true;
    void repoFile(`items/${id}.md`).then(
      (content) => {
        if (live) setState(content === null ? { error: `item not found: ${id}` } : parseFrontmatter(content));
      },
      (e: unknown) => {
        if (live) setState({ error: errText(e) });
      }
    );
    return () => {
      live = false;
    };
  }, [id]);

  if (state === "loading")
    return (
      <Text size="sm" c="dimmed">
        Loading {id}…
      </Text>
    );
  if ("error" in state)
    return (
      <Text size="sm" c="red">
        {state.error}
      </Text>
    );
  const d = state.data;
  return (
    <ItemCard
      slug={id}
      title={asStr(d.title)}
      value={asNum(d.value)}
      status={asStr(d.status) || "open"}
      deadline={asStr(d.deadline) || null}
      body={state.body}
      now={now}
      onNavigate={onNavigate}
    />
  );
}
