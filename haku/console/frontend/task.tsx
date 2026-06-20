import { Anchor, Badge, Button, Group } from "@mantine/core";

import type { Item, OperatorAction } from "./client.ts";
import { CLAUDE_NEW, ITEM_SRC, MAX_DEEPLINK } from "./constants.ts";
import { FeedbackForm } from "./feedback.tsx";
import { renderMarkdown } from "./markdown.ts";

export function clickKey(itemId: string, actionId: string): string {
  return `${itemId} ${actionId}`;
}

// (href, label) for an item's primary action button.
function primaryDeeplink(item: Item): [string, string] {
  const action = item.action;
  if (action.kind === "prepared_prompt") {
    const encoded = encodeURIComponent(action.prompt);
    if (encoded.length <= MAX_DEEPLINK) return [CLAUDE_NEW + encoded, "Hand to Claude →"];
  }
  return [`${ITEM_SRC}/${item.id}.yaml`, "Open item →"];
}

interface ActionProps {
  item: Item;
  action: OperatorAction;
  clicked: ReadonlySet<string>;
  onToggle: (itemId: string, actionId: string) => void;
}

// One operator action attached to an item (``actions[]``). A ``command`` is a
// click/un-click toggle (its state lives in the clicks/ overlay, which Haku reduces
// on its next run); a ``claude_handoff`` is a stateless ``claude.ai/new`` deep-link.
function ActionButton({ item, action, clicked, onToggle }: ActionProps) {
  if (action.kind === "claude_handoff") {
    const encoded = encodeURIComponent(action.prompt);
    const href = encoded.length <= MAX_DEEPLINK ? CLAUDE_NEW + encoded : `${ITEM_SRC}/${item.id}.yaml`;
    return (
      <Button component="a" href={href} variant="default" size="xs" style={{ borderStyle: "dashed" }}>
        {action.label} →
      </Button>
    );
  }
  const isClicked = clicked.has(clickKey(item.id, action.id));
  return (
    <Button
      variant={isClicked ? "filled" : "outline"}
      color="teal"
      size="xs"
      onClick={() => onToggle(item.id, action.id)}
    >
      {isClicked ? `✓ ${action.label}` : action.label}
    </Button>
  );
}

interface TaskProps {
  item: Item;
  clicked: ReadonlySet<string>;
  onToggle: (itemId: string, actionId: string) => void;
}

// One collapsible task. Summary = compact row; the body (Markdown→HTML), operator
// action toggles, the primary action button, and a per-item feedback box live only
// inside the expanded view.
export function TaskCard({ item, clicked, onToggle }: TaskProps) {
  const [href, label] = primaryDeeplink(item);
  const deadline = item.deadline ? item.deadline.slice(0, 10) : null;
  const actions = item.actions ?? [];
  return (
    <details className="group my-2 rounded-lg border border-slate-200 px-3 open:bg-slate-50 dark:border-slate-700 dark:open:bg-slate-800/40 [&_summary::-webkit-details-marker]:hidden">
      <summary className="flex cursor-pointer flex-wrap items-baseline gap-x-2 gap-y-1 py-2">
        <span
          aria-hidden="true"
          className="self-start text-xs text-slate-400 transition-transform group-open:rotate-90"
        >
          ▸
        </span>
        <span className="text-lg font-bold text-teal-600 dark:text-teal-400">{item.value}</span>
        <span className="min-w-0 flex-1 font-semibold break-words">{item.title}</span>
        <span className="flex basis-full flex-wrap items-baseline gap-2 pl-5">
          {deadline && (
            <Badge color="orange" variant="light" size="sm">
              ⏳ {deadline}
            </Badge>
          )}
          <Badge color="gray" variant="light" size="sm">
            {item.action.kind}
          </Badge>
        </span>
      </summary>
      <div className="flex flex-col gap-3 py-2 pl-5">
        <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(item.body) }} />
        {actions.length > 0 && (
          <Group gap="xs">
            {actions.map((action) => (
              <ActionButton key={action.id} item={item} action={action} clicked={clicked} onToggle={onToggle} />
            ))}
          </Group>
        )}
        <Group gap="md" align="center">
          <Button component="a" href={href} color="teal" size="xs">
            {label}
          </Button>
          <Badge color="gray" variant="light">
            {item.source}
          </Badge>
          <Anchor href={`${ITEM_SRC}/${item.id}.yaml`} c="dimmed" size="sm">
            item source →
          </Anchor>
        </Group>
        <FeedbackForm itemId={item.id} minRows={2} placeholder="Feedback on this item…" submitLabel="Send" />
      </div>
    </details>
  );
}
