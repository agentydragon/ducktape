// The CodeMirror 6 grammar for displaying a bash script (see `code_block.tsx`'s `shell` language,
// used by `tool_rendering/kubectl/requests.tsx`'s command rendering).
// `@codemirror/legacy-modes` ships CodeMirror 5's shell mode as a `StreamParser`, which
// `StreamLanguage.define` wraps into a CodeMirror 6 `Language` extension.
import { StreamLanguage } from "@codemirror/language";
import { shell } from "@codemirror/legacy-modes/mode/shell";

/** Singleton — `StreamLanguage.define` output is immutable, so every `CodeBlock language="shell"`
 * reuses the same instance rather than rebuilding the grammar per render. */
export const shellLanguage: ReturnType<typeof StreamLanguage.define> = StreamLanguage.define(shell);
