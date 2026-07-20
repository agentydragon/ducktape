// A minimal CodeMirror 6 grammar for displaying a shell-quoted argv line (see `code_block.tsx`'s
// `shell` language and `hostexec/requests.tsx`'s rendering of `cmd`). hostexec's `cmd` is an argv
// vector executed via `execve` — never actually shell-parsed — so this isn't a real shell parser;
// it just highlights the tokens an operator needs to scan quickly: quoted arguments, `$VAR`/`-flag`
// tokens (common when `cmd` itself invokes a shell, e.g. `["bash", "-lc", "..."]`), and comments.
// `StreamLanguage` (from `@codemirror/language`, already a project dependency) supports exactly
// this "define a `token()` function" style grammar, so no extra CodeMirror language package
// (e.g. `@codemirror/legacy-modes`) is needed.
import { StreamLanguage, type StreamParser } from "@codemirror/language";
import { tags } from "@lezer/highlight";

const SHELL_KEYWORDS = new Set([
  "if",
  "then",
  "else",
  "elif",
  "fi",
  "for",
  "while",
  "until",
  "do",
  "done",
  "case",
  "esac",
  "function",
  "in",
  "select",
]);

const shellStreamParser: StreamParser<null> = {
  token(stream) {
    if (stream.sol() && stream.eat("#")) {
      stream.skipToEnd();
      return "shellComment";
    }
    if (stream.match(/^\$\{[^}]*\}/) || stream.match(/^\$[A-Za-z_][A-Za-z0-9_]*/) || stream.match(/^\$[0-9@*#?$!-]/)) {
      return "shellVariable";
    }
    if (stream.match(/^"(?:[^"\\]|\\.)*"?/) || stream.match(/^'(?:[^'\\]|\\.)*'?/)) {
      return "shellString";
    }
    if (stream.match(/^--?[A-Za-z][\w-]*/)) {
      return "shellFlag";
    }
    if (stream.match(/^\d+(?:\.\d+)?\b/)) {
      return "shellNumber";
    }
    const word = stream.match(/^\S+/);
    if (word) return SHELL_KEYWORDS.has(stream.current()) ? "shellKeyword" : null;
    stream.next();
    return null;
  },
  tokenTable: {
    shellComment: tags.comment,
    shellVariable: tags.variableName,
    shellString: tags.string,
    shellFlag: tags.keyword,
    shellNumber: tags.number,
    shellKeyword: tags.keyword,
  },
};

/** Singleton — `StreamLanguage.define` output is immutable, so every `CodeBlock language="shell"`
 * reuses the same instance rather than rebuilding the grammar per render. */
export const shellLanguage = StreamLanguage.define(shellStreamParser);

/** Shell-quote an argv vector for display (single-quoted when an element needs quoting, with
 * embedded single quotes escaped `'\''`) — the inverse of hostexec's argv splitting, so the
 * operator sees the command the way they'd type it rather than a raw JSON string array. */
export function formatArgv(argv: readonly string[]): string {
  return argv.map(shellQuoteOne).join(" ");
}

const SAFE_UNQUOTED = /^[A-Za-z0-9_.,:/@%+=-]+$/;

function shellQuoteOne(arg: string): string {
  if (arg === "") return "''";
  if (SAFE_UNQUOTED.test(arg)) return arg;
  return `'${arg.replaceAll("'", "'\\''")}'`;
}
