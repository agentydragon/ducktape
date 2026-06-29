import { Anchor, Badge, Button, Card, Collapse, Group, Stack, Text, UnstyledButton } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";

import { openLink } from "./bridge.ts";
import { CLAUDE_NEW, ITEM_SRC, MAX_DEEPLINK } from "./constants.ts";
import { countdown, type Urgency } from "./deadline.ts";
import { FeedbackForm } from "./feedback.tsx";
import { renderMarkdown } from "./markdown.ts";
import type { Item, OperatorAction } from "./types.ts";

export function clickKey(itemId: string, actionId: string): string {
  return `${itemId} ${actionId}`;
}

function itemSrcUrl(item: Item): string {
  return `${ITEM_SRC}/${item.id}.yaml`;
}

// (url, label) for an item's primary action button. A pure FYI item has no action,
// in which case we just link to the item source.
function primaryDeeplink(item: Item): [string, string] {
  const action = item.action;
  if (action?.kind === "prepared_prompt") {
    const encoded = encodeURIComponent(action.prompt);
    if (encoded.length <= MAX_DEEPLINK) return [CLAUDE_NEW + encoded, "Hand to Claude →"];
  }
  return [itemSrcUrl(item), "Open item →"];
}

// Deadline urgency → Mantine palette (one semantic scale; see deadline.ts).
const URGENCY_COLOR: Record<Urgency, string> = { overdue: "red", soon: "orange", later: "gray" };

interface ActionProps {
  item: Item;
  action: OperatorAction;
  clicked: ReadonlySet<string>;
  onToggle: (itemId: string, actionId: string) => void;
}

// One operator action attached to an item (`actions[]`). A `command` is a click/
// un-click toggle (its state lives in the clicks/ overlay, which Haku reduces on its
// next run); a `claude_handoff` is a stateless claude.ai/new deep-link (opened via
// the bridge).
function ActionButton({ item, action, clicked, onToggle }: ActionProps) {
  if (action.kind === "claude_handoff") {
    const encoded = encodeURIComponent(action.prompt);
    const url = encoded.length <= MAX_DEEPLINK ? CLAUDE_NEW + encoded : itemSrcUrl(item);
    return (
      <Button size="xs" variant="default" onClick={() => void openLink(url)}>
        {action.label} →
      </Button>
    );
  }
  const isClicked = clicked.has(clickKey(item.id, action.id));
  return (
    <Button size="xs" color="teal" variant={isClicked ? "filled" : "outline"} onClick={() => onToggle(item.id, action.id)}>
      {isClicked ? `✓ ${action.label}` : action.label}
    </Button>
  );
}

interface TaskProps {
  item: Item;
  clicked: ReadonlySet<string>;
  onToggle: (itemId: string, actionId: string) => void;
  now: number;
}

// One collapsible task card. The header (value, title, badges) toggles the body, which
// holds the Markdown→HTML, operator action toggles, the primary action, and a per-item
// feedback box. ``now`` (ms) drives the live deadline countdown.
export function TaskCard({ item, clicked, onToggle, now }: TaskProps) {
  const [opened, { toggle }] = useDisclosure(false);
  const [url, label] = primaryDeeplink(item);
  const cd = item.deadline ? countdown(item.deadline, now) : null;
  return (
    <Card withBorder radius="md" padding="sm" mb="xs">
      <UnstyledButton onClick={toggle} aria-expanded={opened} style={{ width: "100%" }}>
        <Group gap="xs" wrap="wrap" align="baseline">
          <Text c="dimmed" size="sm" aria-hidden>
            {opened ? "▾" : "▸"}
          </Text>
          <Text fw={700} c="teal" size="lg" fz="1.125rem">
            {item.value}
          </Text>
          <Text fw={600} style={{ flex: 1, minWidth: 0 }}>
            {item.title}
          </Text>
          {cd && (
            <Badge color={URGENCY_COLOR[cd.urgency]} variant="filled" size="sm" style={{ fontVariantNumeric: "tabular-nums" }}>
              ⏳ {cd.text}
            </Badge>
          )}
          {item.action && (
            <Badge color="gray" variant="light" size="sm">
              {item.action.kind}
            </Badge>
          )}
        </Group>
      </UnstyledButton>
      <Collapse expanded={opened}>
        <Stack gap="sm" mt="sm">
          <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(item.body) }} />
          {item.actions.length > 0 && (
            <Group gap="xs">
              {item.actions.map((action) => (
                <ActionButton key={action.id} item={item} action={action} clicked={clicked} onToggle={onToggle} />
              ))}
            </Group>
          )}
          <Group gap="md" align="center">
            <Button size="xs" color="teal" onClick={() => void openLink(url)}>
              {label}
            </Button>
            <Anchor size="sm" c="dimmed" onClick={() => void openLink(itemSrcUrl(item))} style={{ cursor: "pointer" }}>
              item source →
            </Anchor>
          </Group>
          <FeedbackForm itemId={item.id} minRows={2} placeholder="Feedback on this item…" submitLabel="Send" />
        </Stack>
      </Collapse>
    </Card>
  );
}
