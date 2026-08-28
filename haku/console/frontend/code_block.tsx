// Read-only CodeMirror 6 viewer for highlighted, foldable code — the one component behind every
// code-shaped surface in tool-call previews (JSON arguments/results, kubectl YAML manifests,
// hostexec shell commands, plain bodies like an email or a routine instruction). Per-language
// grammars arrive as extensions; the fold gutter collapses long/nested regions. Colors read the
// `--haku-json-*` / `--haku-code-*` CSS variables (see styles.src.css), which flip with the Mantine
// color scheme, so one style adapts to light and dark.
import CodeMirror from "@uiw/react-codemirror";
import { json } from "@codemirror/lang-json";
import { yaml } from "@codemirror/lang-yaml";
import {
  foldEffect,
  foldable,
  foldedRanges,
  HighlightStyle,
  syntaxHighlighting,
  syntaxTreeAvailable,
} from "@codemirror/language";
import type { EditorState, Extension } from "@codemirror/state";
import { EditorView, ViewPlugin, type ViewUpdate } from "@codemirror/view";
import { tags } from "@lezer/highlight";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { shellLanguage } from "./shell_lang";

export type CodeLanguage = "json" | "yaml" | "shell";

const HAKU_HIGHLIGHT = HighlightStyle.define([
  { tag: tags.propertyName, color: "var(--haku-json-key)" },
  { tag: tags.string, color: "var(--haku-json-string)" },
  { tag: tags.number, color: "var(--haku-json-number)" },
  { tag: [tags.bool, tags.keyword, tags.atom, tags.literal], color: "var(--haku-json-literal)" },
  { tag: tags.variableName, color: "var(--haku-json-key)" },
  { tag: tags.comment, color: "var(--haku-code-comment)", fontStyle: "italic" },
]);

// Neutral chrome over the `--haku-code-*` surface. `&` is `.cm-editor`; `theme="none"` on the
// wrapper leaves this as the only theme, so nothing else overrides the vars.
const HAKU_THEME = EditorView.theme({
  "&": {
    backgroundColor: "var(--haku-code-bg)",
    color: "var(--haku-code-fg)",
    border: "1px solid var(--haku-border)",
    borderRadius: "6px",
    fontSize: "0.82rem",
  },
  ".cm-content": {
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
    padding: "0.55rem",
  },
  ".cm-gutters": {
    backgroundColor: "var(--haku-code-bg)",
    color: "var(--haku-code-fg)",
    border: "none",
  },
  "&.cm-focused": { outline: "none" },
});

const LANGUAGE_EXTENSIONS: Record<CodeLanguage, () => Extension> = {
  json: () => json(),
  yaml: () => yaml(),
  shell: () => shellLanguage,
};

// Lines of content that fit in the editor's clipped height — the compact block fills this rather
// than collapsing to a skeleton. `.cm-editor` (`view.dom`) carries the `maxHeight`, so its
// `clientHeight` is the visible box; `defaultLineHeight` accounts for wrapping-free line height.
function visibleLineBudget(view: EditorView): number {
  const height = view.dom.clientHeight;
  const line = view.defaultLineHeight;
  if (height > 0 && line > 0) return Math.max(3, Math.floor(height / line));
  return 10;
}

// The value-blocks of the top-level entries (the foldable children of the outermost container),
// scanned line-by-line via the language's own `foldable`. The whole-document container is returned
// by `foldable` over the full doc; it is skipped (descended past, never folded) so what remains are
// the per-entry blocks — grammar-agnostic, no hardcoded node names. Needs a parsed tree.
function topLevelContainers(state: EditorState): { from: number; to: number; span: number }[] {
  const doc = state.doc;
  const outer = foldable(state, doc.line(1).from, doc.line(doc.lines).to);
  const containers: { from: number; to: number; span: number }[] = [];
  let skipTo = -1;
  for (let i = 1; i <= doc.lines; i++) {
    const line = doc.line(i);
    if (line.from < skipTo) continue;
    const range = foldable(state, line.from, line.to);
    if (!range) continue;
    if (outer && range.from === outer.from && range.to === outer.to) continue; // root container: descend
    const span = doc.lineAt(range.to).number - doc.lineAt(range.from).number + 1;
    containers.push({ from: range.from, to: range.to, span });
    skipTo = range.to;
  }
  return containers;
}

// Which top-level container ranges to fold so leading entries fill the height, as a pure decision
// over a parsed state and a visible-line budget. Walk containers in order; expand one while its span
// fits in the remaining budget MINUS one line per container still after it (the breadth cap — every
// later entry keeps at least its header, so one verbose field can't crowd out the rest); the LAST
// sibling always expands, since folding it would waste the remaining height. `overhead` =
// braces/scalar lines always visible. The ViewPlugin measures the budget and passes it in.
export function chooseCompactFolds(state: EditorState, budget: number): readonly { from: number; to: number }[] {
  const doc = state.doc;
  if (doc.lines <= 1 || doc.lines <= budget) return []; // already fits
  const containers = topLevelContainers(state);
  if (containers.length === 0) return []; // nothing structural to fold; let maxHeight scroll
  const overhead = doc.lines - containers.reduce((sum, c) => sum + c.span, 0);
  let remaining = budget - overhead;
  const folds: { from: number; to: number }[] = [];
  for (let i = 0; i < containers.length; i++) {
    const after = containers.length - i - 1;
    const { from, to, span } = containers[i];
    if (after === 0 || span <= remaining - after) {
      remaining -= span; // expand
    } else {
      folds.push({ from, to }); // collapse a too-big middle sibling to its header line
      remaining -= 1;
    }
  }
  return folds;
}

function foldCompact(view: EditorView): void {
  const folds = chooseCompactFolds(view.state, visibleLineBudget(view));
  if (folds.length) view.dispatch({ effects: folds.map((f) => foldEffect.of(f)) });
}

// Fold once the parse + layout settle, and never clobber a fold the operator toggled. Polls via rAF
// until the syntax tree is ready rather than blocking, so many compact blocks mounting together
// (the history page) don't jank.
function compactFoldExtension(): Extension {
  return ViewPlugin.fromClass(
    class {
      private folded = false;
      private attempts = 0;
      constructor(private view: EditorView) {
        this.schedule();
      }
      update(update: ViewUpdate): void {
        if (update.docChanged) {
          this.folded = false;
          this.schedule();
        }
      }
      private schedule(): void {
        if (this.folded || this.attempts++ > 30) return;
        requestAnimationFrame(() => {
          if (this.folded) return;
          const { view } = this;
          if (foldedRanges(view.state).size > 0) {
            this.folded = true; // already folded (operator interaction) — leave it
            return;
          }
          if (syntaxTreeAvailable(view.state, view.state.doc.length)) {
            foldCompact(view);
            this.folded = true;
          } else {
            this.schedule(); // parse not done yet; retry next frame
          }
        });
      }
    }
  );
}

// How far outside the viewport a block still mounts its editor, so scrolling reveals finished
// blocks rather than placeholders resolving under the pointer.
const MOUNT_MARGIN_PX = 600;

// The placeholder's reserved height, from the mounted editor's own metrics: HAKU_THEME's 0.82rem
// font at CodeMirror's ~1.4 line-height, plus `.cm-content`'s 0.55rem padding top and bottom.
const PLACEHOLDER_LINE_REM = 1.15;
const PLACEHOLDER_PADDING_REM = 1.1;

/** Whether this block has come near the viewport yet — the gate on building its editor.
 *
 * An `EditorView` is expensive to construct (DOM, a lezer parse, and for a compact block a
 * per-frame poll until the fold pass can run), and the history page holds one per row: mounting
 * every one up front froze the tab, 500 rows costing ~15s of blocked main thread. Off-screen rows
 * cost a placeholder div until scrolled to.
 *
 * Latches on first intersection and stops observing: an editor that scrolls away stays mounted,
 * since tearing it down would lose the operator's own fold/scroll state within it. Where there is
 * no `IntersectionObserver` (jsdom under vitest) every block mounts immediately. */
function useNearViewport(): { ref: (node: HTMLDivElement | null) => void; near: boolean } {
  const [near, setNear] = useState(typeof IntersectionObserver === "undefined");
  const observerRef = useRef<IntersectionObserver | null>(null);
  useEffect(() => () => observerRef.current?.disconnect(), []);
  const ref = useCallback(
    (node: HTMLDivElement | null) => {
      observerRef.current?.disconnect();
      observerRef.current = null;
      if (!node || near) return;
      const observer = new IntersectionObserver(
        (entries) => {
          if (!entries.some((entry) => entry.isIntersecting)) return;
          observer.disconnect();
          setNear(true);
        },
        { rootMargin: `${MOUNT_MARGIN_PX}px` }
      );
      observer.observe(node);
      observerRef.current = observer;
    },
    [near]
  );
  return { ref, near };
}

/** Read-only, foldable, syntax-highlighted code. Omit `language` for plain text (an email body, a
 * routine instruction) — same neutral chrome, no grammar and so no fold gutter. `compact` (only
 * meaningful with a `language`) auto-folds so the block fills its height with leading entries
 * instead of overflowing; `lineNumbers` is off by default, on for long detailed surfaces.
 *
 * The editor itself is built only once the block nears the viewport (see `useNearViewport`); until
 * then it reserves roughly the height it will take, so arriving at it is not a layout jump. */
export function CodeBlock({
  value,
  language,
  compact = false,
  lineNumbers = false,
}: {
  value: string;
  language?: CodeLanguage;
  compact?: boolean;
  lineNumbers?: boolean;
}): JSX.Element {
  const extensions = useMemo<Extension[]>(() => {
    const exts: Extension[] = [EditorView.lineWrapping, syntaxHighlighting(HAKU_HIGHLIGHT), HAKU_THEME];
    if (language) {
      exts.unshift(LANGUAGE_EXTENSIONS[language]());
      if (compact) exts.push(compactFoldExtension());
    }
    return exts;
  }, [language, compact]);
  const { ref, near } = useNearViewport();
  if (!near) {
    // Ten lines is where `maxHeight` below caps the mounted editor, so the placeholder never needs
    // to reserve more than that.
    const lines = Math.min(value.split("\n").length, 10);
    const height = `${lines * PLACEHOLDER_LINE_REM + PLACEHOLDER_PADDING_REM}rem`;
    return <div ref={ref} className="haku-codeblock-placeholder" style={{ height }} />;
  }
  return (
    <CodeMirror
      className="haku-codeblock"
      value={value}
      theme="none"
      readOnly
      editable={false}
      extensions={extensions}
      maxHeight="14rem"
      basicSetup={{
        lineNumbers,
        foldGutter: !!language,
        highlightActiveLine: false,
        highlightActiveLineGutter: false,
      }}
    />
  );
}
