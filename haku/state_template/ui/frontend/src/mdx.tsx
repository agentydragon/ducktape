import { Anchor, Text } from "@mantine/core";
import DOMPurify from "dompurify";
import { marked } from "marked";
import type { ReactNode } from "react";
import { createElement, Fragment, useMemo } from "react";

import { Choice, Choices, Feedback, Handoff, Launch, SignalToggle, ToolCall } from "./affordances.tsx";
import { errText } from "./errors.ts";
import { ItemCardById } from "./item_card.tsx";
import { ImprovementBoard } from "./improvement_board.tsx";
import { isExternal, resolveRepoPath } from "./mdx_links.ts";
import { Callout, StatusBadge } from "./widgets.tsx";
import type { CalloutKind } from "./widgets.tsx";

// Widget tags authored content may embed (garden.md documents the literal-attribute syntax, e.g.
// `<statusbadge status="open" color="teal">`). HTML tag/attribute names are case-insensitive, so
// the DOM always hands these back lowercased regardless of how they were written. Attributes are
// always plain strings (nothing evaluates the markdown to compute a richer value).
interface WidgetProps {
  children?: ReactNode;
  kind?: string;
  title?: string;
  status?: string;
  color?: string;
  prompt?: string;
  label?: string;
  text?: string;
  item?: string;
  value?: string;
  scope?: string;
  field?: string;
  id?: string;
  request?: string;
  rawText?: string; // the element's plain text content — for a prompt authored *inside* the tag
}

const WIDGET_COMPONENTS: Record<string, (props: WidgetProps) => ReactNode> = {
  callout: (p) => (
    <Callout kind={p.kind as CalloutKind | undefined} title={p.title}>
      {p.children}
    </Callout>
  ),
  statusbadge: (p) => <StatusBadge status={p.status ?? ""} color={p.color} />,
  // Data-driven view widget: queries the memory/improvements/ collection and renders it (no
  // literal-attribute props — the data comes from the collection, not the tag). Authored as
  // `<improvement-board></improvement-board>`.
  "improvement-board": () => <ImprovementBoard />,
  // Affordance widgets (see affordances.tsx): reviewed action buttons any item/note body can embed,
  // each wrapping an already-gated capability.
  //   `<handoff prompt="…" label="…"></handoff>`  — hand off to a fresh claude.ai conversation
  //   `<launch prompt="…" label="…"></launch>`    — ask the shell to launch a Haku run (shell confirms)
  //   `<feedback text="…" label="…" item="…">`    — one-click canned feedback into the trace
  //   `<choices prompt="…" item="…"><choice value="a"/>…</choices>` — single-select outcome capture
  //   `<signal-toggle scope="…" field="…"><choice value="a"/>…</signal-toggle>` — stateful slot
  //   `<tool-call request="…">`                    — submit tool_requests/<id>.yaml via console
  // Prompt from the `prompt` attribute (short/inline) or, if absent, the tag's own text content
  // (a fenced code block inside `<handoff>…</handoff>` — how long/multi-line prompts are authored).
  handoff: (p) => <Handoff prompt={p.prompt || (p.rawText ?? "").trim()} label={p.label} />,
  launch: (p) => <Launch prompt={p.prompt ?? ""} label={p.label} />,
  feedback: (p) => <Feedback text={p.text ?? ""} label={p.label} item={p.item} />,
  "tool-call": (p) => <ToolCall request={p.request} label={p.label} />,
  choices: (p) => (
    <Choices prompt={p.prompt} item={p.item}>
      {p.children}
    </Choices>
  ),
  choice: (p) => <Choice value={p.value}>{p.children}</Choice>,
  "signal-toggle": (p) => (
    <SignalToggle scope={p.scope} field={p.field ?? ""} prompt={p.prompt}>
      {p.children}
    </SignalToggle>
  ),
  // `<item-card id="slug">` — render one item's live card inline (by-reference embed in a note).
  "item-card": (p) => <ItemCardById id={p.id ?? ""} now={Date.now()} />,
};

// HTML boolean attributes marked's GFM task-list output relies on (`- [ ]` → `<input type=checkbox
// disabled>`) — presence means `true`, there is no meaningful string value to preserve.
const BOOLEAN_ATTRS = new Set(["disabled", "checked", "readonly", "required", "selected", "multiple"]);

function attrsToProps(el: Element): Record<string, string | boolean> {
  const props: Record<string, string | boolean> = {};
  for (const attr of Array.from(el.attributes)) {
    const name = attr.name === "class" ? "className" : attr.name === "for" ? "htmlFor" : attr.name;
    props[name] = BOOLEAN_ATTRS.has(attr.name) ? true : attr.value;
  }
  if ("checked" in props) props.readOnly = true; // React requires this on a controlled checkbox.
  return props;
}

// Turn a sanitized DOM node into a React tree: standard elements pass through as themselves, `a`
// is intercepted so internal `.md`/`.mdx` links navigate the garden in-app (external links open
// in a new tab), and the two widget tags become their registered component. This replaces
// `@mdx-js/mdx`'s runtime `evaluate()`, which compiled content to a function via `new Function` —
// blocked by the gateway's CSP (`script-src 'self'`, no `unsafe-eval`; base #2711). Nothing here
// evaluates a string as code: it's a plain recursive walk over an already-parsed, already-sanitized
// DOM tree, so no CSP directive can block it.
function domToReact(
  node: ChildNode,
  basePath: string | undefined,
  onNavigate: ((path: string) => void) | undefined
): ReactNode {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent;
  if (node.nodeType !== Node.ELEMENT_NODE) return null;
  const el = node as Element;
  const tag = el.tagName.toLowerCase();
  const children = Array.from(el.childNodes).map((c, i) => (
    <Fragment key={i}>{domToReact(c, basePath, onNavigate)}</Fragment>
  ));

  if (tag === "a") {
    const href = el.getAttribute("href") ?? "";
    const target = href.replace(/[#?].*$/, "");
    if (onNavigate && !isExternal(href) && !href.startsWith("#") && /\.mdx?$/.test(target)) {
      const repoPath = resolveRepoPath(basePath, href);
      // Keep the href (a real, focusable link) but intercept the click to navigate in-app.
      return (
        <Anchor
          href={href}
          onClick={(e) => {
            e.preventDefault();
            onNavigate(repoPath);
          }}
        >
          {children}
        </Anchor>
      );
    }
    const ext = isExternal(href);
    return (
      <Anchor href={href} target={ext ? "_blank" : undefined} rel={ext ? "noreferrer" : undefined}>
        {children}
      </Anchor>
    );
  }

  const widget = WIDGET_COMPONENTS[tag];
  // Also hand widgets the element's raw text (e.g. a `<handoff>` whose prompt is a fenced code
  // block *inside* the tag — long/multi-line prompts can't be a literal attribute).
  if (widget) return widget({ ...attrsToProps(el), children, rawText: el.textContent ?? undefined } as WidgetProps);

  // A dynamic HTML tag name with dynamic attributes has no static prop type to check against —
  // this is the one boundary where "trust the sanitized DOM" replaces a real type.
  return createElement(tag, attrsToProps(el) as never, ...children);
}

// Render a markdown string to sanitized, navigable React: `marked` (CommonMark + GFM, no eval)
// compiles to an HTML string, `DOMPurify` sanitizes it (extended to keep the widget tags), then
// `domToReact` rebuilds it as React so internal links and widgets stay interactive. `basePath` is
// the rendered file's repo path (for relative-link resolution); pass `onNavigate` to make internal
// links open in-app. This is what backs the garden (run notes, memory/procedures files) — see
// `procedures/garden.md`.
export function Mdx({
  source,
  basePath,
  onNavigate,
}: {
  source: string;
  basePath?: string;
  onNavigate?: (path: string) => void;
}) {
  // Parsing is synchronous now (no eval → no compile step to await), so a plain memo replaces the
  // old evaluate()-then-setState effect: no loading state, no stale-closure cleanup to manage.
  const result = useMemo((): { tree: ReactNode } | { error: string } => {
    try {
      const html = marked.parse(source, { async: false, gfm: true }) as string;
      const clean = DOMPurify.sanitize(html, {
        ADD_TAGS: Object.keys(WIDGET_COMPONENTS),
        // Custom widget attributes DOMPurify would otherwise strip (they aren't standard HTML
        // attributes). Every literal-attribute prop a WIDGET_COMPONENTS entry reads must be listed
        // here or it silently arrives as undefined — the widget then renders its fallback (e.g. an
        // empty handoff prompt). Keep in sync with the props the registry reads.
        ADD_ATTR: ["kind", "status", "color", "prompt", "label", "text", "item", "field", "request"],
      });
      const doc = new DOMParser().parseFromString(clean, "text/html");
      const tree = Array.from(doc.body.childNodes).map((n, i) => (
        <Fragment key={i}>{domToReact(n, basePath, onNavigate)}</Fragment>
      ));
      return { tree };
    } catch (e) {
      return { error: errText(e) };
    }
  }, [source, basePath, onNavigate]);

  if ("error" in result)
    return (
      <Text c="red" size="sm">
        Couldn't render this content: {result.error}
      </Text>
    );
  return <div className="md">{result.tree}</div>;
}
