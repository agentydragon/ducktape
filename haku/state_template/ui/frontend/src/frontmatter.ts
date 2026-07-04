import { parse } from "yaml";

import { logger } from "./log.ts";

// Split a Haku-authored garden doc into its leading `---` YAML front-matter and the markdown
// body. The front-matter is parsed with a real YAML parser (the `yaml` package) — the same
// language `validate_state.py` (PyYAML) validates it with, so the two agree. `data` is untyped
// on purpose (YAML yields anything); callers read the fields they need defensively.

const log = logger("frontmatter");

export interface FrontMatter {
  data: Record<string, unknown>;
  body: string;
}

const FENCE = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/;

export function parseFrontmatter(text: string): FrontMatter {
  const m = FENCE.exec(text);
  if (!m) return { data: {}, body: text };
  let parsed: unknown;
  try {
    parsed = parse(m[1]);
  } catch (e) {
    // Malformed YAML — the validate-state CI gate blocks this from reaching prod; degrade to empty
    // front-matter so one bad file never blanks the whole board (the widget then skips it), but log
    // it so a file that slips through is visible rather than silently empty.
    log.warn("malformed YAML front-matter, treating as empty", e);
    return { data: {}, body: m[2] };
  }
  const data = parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : {};
  return { data, body: m[2] };
}
