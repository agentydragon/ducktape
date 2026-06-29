import { openLink } from "./bridge.ts";
import { CLAUDE_NEW, ITEM_SRC, MAX_DEEPLINK } from "./constants.ts";
import { countdown } from "./deadline.ts";
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

// All outbound navigation goes through the shell's openLink bridge (this UI is in a
// popup-less sandboxed iframe; bare anchors/window.open are blocked).
function BridgeLink({ url, className, children }: { url: string; className?: string; children: React.ReactNode }) {
  return (
    <button
      type="button"
      className={className}
      onClick={() => {
        void openLink(url);
      }}
    >
      {children}
    </button>
  );
}

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
      <BridgeLink url={url} className="btn btn-handoff">
        {action.label} →
      </BridgeLink>
    );
  }
  const isClicked = clicked.has(clickKey(item.id, action.id));
  return (
    <button
      type="button"
      className={isClicked ? "btn btn-toggle btn-toggle-on" : "btn btn-toggle"}
      onClick={() => onToggle(item.id, action.id)}
    >
      {isClicked ? `✓ ${action.label}` : action.label}
    </button>
  );
}

interface TaskProps {
  item: Item;
  clicked: ReadonlySet<string>;
  onToggle: (itemId: string, actionId: string) => void;
  now: number;
}

// One collapsible task. Summary = compact row; the body (Markdown→HTML), operator
// action toggles, the primary action button, and a per-item feedback box live only
// inside the expanded view. ``now`` (ms) drives the live deadline countdown.
export function TaskCard({ item, clicked, onToggle, now }: TaskProps) {
  const [url, label] = primaryDeeplink(item);
  const cd = item.deadline ? countdown(item.deadline, now) : null;
  return (
    <details className="card">
      <summary>
        <span className="marker" aria-hidden="true">
          ▸
        </span>
        <span className="value">{item.value}</span>
        <span className="title">{item.title}</span>
        <span className="badges">
          {cd && <span className={`badge badge-deadline badge-${cd.urgency}`}>⏳ {cd.text}</span>}
          {item.action && <span className="badge badge-kind">{item.action.kind}</span>}
        </span>
      </summary>
      <div className="card-body">
        <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(item.body) }} />
        {item.actions.length > 0 && (
          <div className="actions">
            {item.actions.map((action) => (
              <ActionButton key={action.id} item={item} action={action} clicked={clicked} onToggle={onToggle} />
            ))}
          </div>
        )}
        <div className="primary-row">
          <BridgeLink url={url} className="btn btn-primary">
            {label}
          </BridgeLink>
          <BridgeLink url={itemSrcUrl(item)} className="linklike">
            item source →
          </BridgeLink>
        </div>
        <FeedbackForm itemId={item.id} minRows={2} placeholder="Feedback on this item…" submitLabel="Send" />
      </div>
    </details>
  );
}
