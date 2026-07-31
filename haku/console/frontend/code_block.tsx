// Read-only CodeMirror 6 viewer for highlighted, foldable code — the one component behind every
// code-shaped surface in tool-call previews (JSON arguments/results, kubectl YAML manifests,
// hostexec shell commands, plain bodies like an email or a routine instruction). Per-language
// grammars arrive as extensions; the fold gutter collapses long/nested regions. Colors read the
// `--haku-json-*` / `--haku-code-*` CSS variables (see styles.src.css), which flip with the
// Mantine color scheme, so one style adapts to light and dark and keeps the muted palette the
// former highlight.js hues used.
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
import { useMemo } from "react";

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

// Neutral chrome over the same `--haku-code-*` surface the old `<pre>` blocks used. `&` is
// `.cm-editor`; `theme="none"` on the wrapper leaves this as the only theme, so nothing else
// overrides the vars. Line wrapping stands in for the old `white-space: pre-wrap`.
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

// Compact policy, as a pure decision: given a parsed state and a visible-line budget, which
// top-level container ranges to fold so leading entries fill the height. Walk containers in order;
// expand one while its span fits in the remaining budget MINUS one line per container still after it
// (the breadth cap — every later entry keeps at least its header, so one verbose field can't crowd
// out the rest); the LAST sibling always expands (folding it would waste the remaining height, so it
// scrolls instead). `overhead` = braces/scalar lines always visible. No DOM, no dispatch — passed a
// budget by the ViewPlugin (which measures it) and unit-tested headlessly.
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

// ViewPlugin (silverbullet/flint-chart pattern): fold once the parse + layout settle, and never
// clobber a fold the operator toggled. Polls via rAF until the syntax tree is ready rather than
// blocking, so many compact blocks mounting together (the history page) don't jank.
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

/** Read-only, foldable, syntax-highlighted code. Omit `language` for plain text (an email body, a
 * routine instruction) — same neutral chrome, no grammar and so no fold gutter. `compact` (only
 * meaningful with a `language`) auto-folds so the block fills its height with leading entries
 * instead of overflowing; `lineNumbers` is off by default, on for long detailed surfaces. */
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
}) {
  const extensions = useMemo<Extension[]>(() => {
    const exts: Extension[] = [EditorView.lineWrapping, syntaxHighlighting(HAKU_HIGHLIGHT), HAKU_THEME];
    if (language) {
      exts.unshift(LANGUAGE_EXTENSIONS[language]());
      if (compact) exts.push(compactFoldExtension());
    }
    return exts;
  }, [language, compact]);
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
