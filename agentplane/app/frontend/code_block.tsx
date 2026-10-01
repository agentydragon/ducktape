import CodeMirror from "@uiw/react-codemirror";
import { json } from "@codemirror/lang-json";
import { yaml } from "@codemirror/lang-yaml";
import { HighlightStyle, StreamLanguage, syntaxHighlighting } from "@codemirror/language";
import { go } from "@codemirror/legacy-modes/mode/go";
import { javascript, typescript } from "@codemirror/legacy-modes/mode/javascript";
import { protobuf } from "@codemirror/legacy-modes/mode/protobuf";
import { python } from "@codemirror/legacy-modes/mode/python";
import { rust } from "@codemirror/legacy-modes/mode/rust";
import { shell } from "@codemirror/legacy-modes/mode/shell";
import { nix } from "@replit/codemirror-lang-nix";
import type { Extension } from "@codemirror/state";
import { Decoration, EditorView, highlightSpecialChars, WidgetType } from "@codemirror/view";
import { tags } from "@lezer/highlight";
import { type JSX, useCallback, useEffect, useMemo, useRef, useState } from "react";

import "./code_block.css";

export type Language =
  "bash" | "go" | "javascript" | "json" | "nix" | "protobuf" | "python" | "rust" | "typescript" | "yaml";

// Reuse CodeMirror language support instead of translating another highlighter's HTML into
// decorations. JSON/YAML/Nix use language packages; the remaining curated grammars are the
// CodeMirror legacy stream modes, as in Haku's shared read-only code viewer.
const LEGACY_LANGUAGES = {
  bash: StreamLanguage.define(shell),
  go: StreamLanguage.define(go),
  javascript: StreamLanguage.define(javascript),
  protobuf: StreamLanguage.define(protobuf),
  python: StreamLanguage.define(python),
  rust: StreamLanguage.define(rust),
  typescript: StreamLanguage.define(typescript),
};

const LANGUAGE_EXTENSIONS: Record<Language, () => Extension> = {
  bash: () => LEGACY_LANGUAGES.bash,
  go: () => LEGACY_LANGUAGES.go,
  javascript: () => LEGACY_LANGUAGES.javascript,
  json: () => json(),
  nix: () => nix(),
  protobuf: () => LEGACY_LANGUAGES.protobuf,
  python: () => LEGACY_LANGUAGES.python,
  rust: () => LEGACY_LANGUAGES.rust,
  typescript: () => LEGACY_LANGUAGES.typescript,
  yaml: () => yaml(),
};

const REGISTERED_LANGUAGES: ReadonlySet<string> = new Set(Object.keys(LANGUAGE_EXTENSIONS));

const CODE_HIGHLIGHT = HighlightStyle.define([
  { tag: tags.propertyName, color: "var(--agentplane-code-key)" },
  { tag: tags.string, color: "var(--agentplane-code-string)" },
  { tag: tags.number, color: "var(--agentplane-code-number)" },
  { tag: [tags.bool, tags.atom, tags.literal], color: "var(--agentplane-code-literal)" },
  { tag: [tags.keyword, tags.controlKeyword], color: "var(--agentplane-code-keyword)" },
  { tag: tags.comment, color: "var(--agentplane-code-comment)", fontStyle: "italic" },
  { tag: tags.meta, color: "var(--agentplane-code-meta)" },
  { tag: tags.variableName, color: "var(--agentplane-code-variable)" },
  { tag: [tags.function(tags.variableName), tags.className], color: "var(--agentplane-code-title)" },
]);

/** Whether `language` names one of the grammars this widget can highlight. */
export function isRegisteredLanguage(language: string): language is Language {
  return REGISTERED_LANGUAGES.has(language);
}

// Render Unicode bidi controls, default-ignorable code points (including zero-width characters),
// and C0/C1 controls as widgets. Newlines and tabs remain normal layout characters. The document
// itself is never rewritten, so copying from the read-only editor retains the original value.
export const VISIBLE_SPECIAL_CHARS: RegExp =
  /[\p{Bidi_Control}\p{Default_Ignorable_Code_Point}\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\u2028\u2029\uFFF9-\uFFFC]/gu;
const BIDI_CONTROLS = /\p{Bidi_Control}/u;
const CONTROL_NAMES: ReadonlyMap<number, string> = new Map([
  [0x0000, "NUL"],
  [0x0001, "SOH"],
  [0x0002, "STX"],
  [0x0003, "ETX"],
  [0x0004, "EOT"],
  [0x0005, "ENQ"],
  [0x0006, "ACK"],
  [0x0007, "BEL"],
  [0x0008, "BS"],
  [0x000b, "VT"],
  [0x000c, "FF"],
  [0x000e, "SO"],
  [0x000f, "SI"],
  [0x0010, "DLE"],
  [0x0011, "DC1"],
  [0x0012, "DC2"],
  [0x0013, "DC3"],
  [0x0014, "DC4"],
  [0x0015, "NAK"],
  [0x0016, "SYN"],
  [0x0017, "ETB"],
  [0x0018, "CAN"],
  [0x0019, "EM"],
  [0x001a, "SUB"],
  [0x001b, "ESC"],
  [0x001c, "FS"],
  [0x001d, "GS"],
  [0x001e, "RS"],
  [0x001f, "US"],
  [0x007f, "DEL"],
  [0x00ad, "SHY"],
  [0x061c, "ALM"],
  [0x200b, "ZWSP"],
  [0x200c, "ZWNJ"],
  [0x200d, "ZWJ"],
  [0x200e, "LRM"],
  [0x200f, "RLM"],
  [0x202a, "LRE"],
  [0x202b, "RLE"],
  [0x202c, "PDF"],
  [0x202d, "LRO"],
  [0x202e, "RLO"],
  [0x2060, "WJ"],
  [0x2066, "LRI"],
  [0x2067, "RLI"],
  [0x2068, "FSI"],
  [0x2069, "PDI"],
  [0xfeff, "BOM"],
]);

function codePointLabel(code: number): string {
  return CONTROL_NAMES.get(code) ?? `U+${code.toString(16).toUpperCase().padStart(4, "0")}`;
}

export function renderSpecialChar(code: number, description: string | null, _placeholder: string): HTMLElement {
  const codePoint = `U+${code.toString(16).toUpperCase().padStart(4, "0")}`;
  const isBidi = BIDI_CONTROLS.test(String.fromCodePoint(code));
  const isControl = code <= 0x001f || (code >= 0x007f && code <= 0x009f);
  const kind = isBidi
    ? "bidirectional control"
    : isControl
      ? "control character"
      : code === 0x2028
        ? "line separator"
        : code === 0x2029
          ? "paragraph separator"
          : "default-ignorable character";
  const label = codePointLabel(code);
  const marker = document.createElement("span");
  marker.className = `cm-agentplane-special-char ${isBidi ? "cm-agentplane-special-char-bidi" : isControl ? "cm-agentplane-special-char-control" : "cm-agentplane-special-char-ignorable"}`;
  marker.setAttribute("aria-label", `${kind}: ${description ?? label}, ${codePoint}`);
  marker.title = `${kind}: ${label} (${codePoint})`;
  marker.textContent = `⟦${label}⟧`;
  return marker;
}

class StreamingCursor extends WidgetType {
  eq(other: WidgetType): boolean {
    return other === this;
  }
  toDOM(): HTMLElement {
    const cursor = document.createElement("span");
    cursor.className = "agentplane-streaming-cursor";
    cursor.setAttribute("role", "img");
    cursor.setAttribute("aria-label", "Streaming");
    cursor.setAttribute("data-character", "|");
    return cursor;
  }
}

const STREAMING_CURSOR_WIDGET = new StreamingCursor();

function streamingCursorDecoration(offset: number): Extension {
  return EditorView.decorations.of(
    Decoration.set([Decoration.widget({ widget: STREAMING_CURSOR_WIDGET, side: 1 }).range(offset)])
  );
}

const CODE_THEME = EditorView.theme({
  "&": {
    backgroundColor: "var(--mantine-color-body)",
    border: "1px solid var(--mantine-color-default-border)",
    borderRadius: "var(--mantine-radius-sm)",
    color: "var(--mantine-color-text)",
    fontSize: "var(--mantine-font-size-sm)",
  },
  ".cm-content": {
    fontFamily: "var(--mantine-font-family-monospace)",
    padding: "var(--mantine-spacing-xs)",
  },
  ".cm-scroller": { fontFamily: "inherit" },
  "&.cm-focused": { outline: "none" },
});

const LABEL_CODE_THEME = EditorView.theme({
  "&": {
    backgroundColor: "transparent",
    border: "none",
    borderRadius: "0",
    color: "var(--mantine-color-text)",
    fontSize: "var(--mantine-font-size-sm)",
    fontWeight: "600",
  },
  ".cm-content": {
    fontFamily: "var(--mantine-font-family-monospace)",
    padding: 0,
  },
  ".cm-scroller": { fontFamily: "inherit" },
  "&.cm-focused": { outline: "none" },
});

const MUTED_CODE_THEME = EditorView.theme({
  "&": {
    backgroundColor: "transparent",
    border: "none",
    borderRadius: "0",
    color: "var(--mantine-color-dimmed)",
    fontSize: "var(--mantine-font-size-xs)",
  },
  ".cm-content": {
    fontFamily: "var(--mantine-font-family-monospace)",
    padding: 0,
  },
  ".cm-scroller": { fontFamily: "inherit" },
  "&.cm-focused": { outline: "none" },
});

const MOUNT_MARGIN_PX = 600;
const PLACEHOLDER_LINE_REM = 1.25;

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

function MountedCodeBlock({
  text,
  language,
  streamingCursorOffset,
  presentation,
}: {
  text: string;
  language?: Language;
  streamingCursorOffset?: number;
  presentation: "block" | "label" | "muted";
}): JSX.Element {
  const extensions = useMemo(() => {
    const result: Extension[] = [
      EditorView.lineWrapping,
      highlightSpecialChars({ specialChars: VISIBLE_SPECIAL_CHARS, render: renderSpecialChar }),
      syntaxHighlighting(CODE_HIGHLIGHT),
      presentation === "label" ? LABEL_CODE_THEME : presentation === "muted" ? MUTED_CODE_THEME : CODE_THEME,
    ];
    if (language) result.unshift(LANGUAGE_EXTENSIONS[language]());
    if (streamingCursorOffset !== undefined) result.push(streamingCursorDecoration(streamingCursorOffset));
    return result;
  }, [language, streamingCursorOffset, presentation]);

  return (
    <CodeMirror
      className={`agentplane-code-block agentplane-code-block-${presentation}`}
      value={text}
      theme="none"
      readOnly
      editable={false}
      extensions={extensions}
      basicSetup={false}
    />
  );
}

/** Read-only code and text viewer for Agentplane payloads. Syntax tokens, hidden Unicode markers,
 * and copy all share the same original document. Editor views mount near the viewport because
 * transcript history can contain many code-bearing rows. */
export function CodeBlock({
  text,
  language,
  streamingCursorOffset,
  presentation = "block",
}: {
  text: string;
  language?: Language;
  streamingCursorOffset?: number;
  presentation?: "block" | "label" | "muted";
}): JSX.Element {
  const { ref, near } = useNearViewport();
  if (!near) {
    const lineCount = Math.max(1, text.split("\n").length);
    return (
      <div
        ref={ref}
        className={`agentplane-code-block-placeholder${presentation !== "block" ? ` agentplane-code-block-placeholder-${presentation}` : ""}`}
        style={{ height: `${lineCount * PLACEHOLDER_LINE_REM}rem` }}
      />
    );
  }
  return (
    <MountedCodeBlock
      text={text}
      language={language}
      streamingCursorOffset={streamingCursorOffset}
      presentation={presentation}
    />
  );
}
