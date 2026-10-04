// The tool calls that run a shell command, in the two shapes the harnesses give them: Claude's
// `Bash` tool and Codex's `commandExecution` item. Their arguments are parsed into one `CommandCall`
// for the thread to draw as a command; any call that does not parse is drawn as its JSON.
import { z } from "zod";

import { parsedJson } from "../parsed_json";

/** A shell command call, as the thread shows it. */
export interface CommandCall {
  /** What to call the tool. */
  label: string;
  /** The one line that stands for the call while it is folded. */
  summary: string;
  /** What the model said the command is for; only Claude's tool takes one. */
  description?: string;
  /** The command, as the shell is given it. */
  command: string;
  /** The settings that change how it runs. */
  notes: string[];
}

// Claude Code's `Bash` tool input. Strict, because the thread draws only these: a call carrying any
// other argument shows as its JSON, so nothing it would run with goes unseen.
const claudeBash = z.strictObject({
  command: z.string().min(1),
  description: z.string().optional(),
  timeout: z.int().positive().optional(),
  run_in_background: z.boolean().optional(),
  dangerouslyDisableSandbox: z.boolean().optional(),
});

// What `agentplane/runner/codex.py` records for a `commandExecution` item.
const codexCommand = z.strictObject({
  command: z.string().min(1),
  cwd: z.string(),
});

// A summary is one line however long the command is; the line clips what does not fit.
const SUMMARY_CHARACTERS = 300;

/** `text` on one line, its lines and the runs of whitespace between them reduced to single spaces. */
export function oneLine(text: string): string {
  return text.replace(/\s+/g, " ").trim().slice(0, SUMMARY_CHARACTERS);
}

/** `argumentsJson` as the call to the harness's shell tool `toolName`, or `null` for any other tool,
 * for arguments that are not JSON (a call still streaming in), and for arguments the tool does not
 * take whole. */
export function parseCommandCall(toolName: string, argumentsJson: string): CommandCall | null {
  const value = parsedJson(argumentsJson);
  switch (toolName) {
    case "Bash": {
      const parsed = claudeBash.safeParse(value);
      if (!parsed.success) return null;
      const { command, description, timeout, run_in_background, dangerouslyDisableSandbox } = parsed.data;
      return {
        label: "Bash",
        summary: oneLine(description || command),
        description: description || undefined,
        command,
        notes: [
          ...(timeout === undefined ? [] : [`Timeout ${timeout} ms`]),
          ...(run_in_background ? ["Runs in the background"] : []),
          ...(dangerouslyDisableSandbox ? ["Sandbox disabled for this call"] : []),
        ],
      };
    }
    case "commandExecution": {
      const parsed = codexCommand.safeParse(value);
      if (!parsed.success) return null;
      // Codex records the argv it ran, joined: nearly always a shell given the script to run.
      const wrapped = shellInvocation(parsed.data.command);
      return {
        label: "Shell",
        summary: oneLine(wrapped?.script ?? parsed.data.command),
        command: wrapped?.script ?? parsed.data.command,
        notes: [...(wrapped ? [`Run by ${wrapped.invocation}`] : []), `In ${parsed.data.cwd}`],
      };
    }
    default:
      return null;
  }
}

const SHELLS = new Set(["bash", "sh", "zsh", "dash"]);

/** `commandLine` as a shell run on a script (`/bin/bash -lc "…"`), or `null` when it is anything
 * else, whether another program, more arguments, or a line that does not split. */
export function shellInvocation(commandLine: string): { invocation: string; script: string } | null {
  const words = shellWords(commandLine);
  if (words === null || words.length !== 3) return null;
  const [shell, flags, script] = words;
  if (!SHELLS.has(shell.slice(shell.lastIndexOf("/") + 1)) || !/^-[a-z]*c$/.test(flags)) return null;
  return { invocation: `${shell} ${flags}`, script };
}

/** `line` split into words the way a POSIX shell does: quotes and backslashes group and escape, and
 * `null` where they never close. It does not expand or interpret anything else. */
function shellWords(line: string): string[] | null {
  const words: string[] = [];
  // `null` between words; a word of nothing but an empty quote is `""`.
  let word: string | null = null;
  for (let index = 0; index < line.length; index++) {
    const character = line[index];
    if (/\s/.test(character)) {
      if (word !== null) words.push(word);
      word = null;
      continue;
    }
    word ??= "";
    if (character === "'") {
      const end = line.indexOf("'", index + 1);
      if (end < 0) return null;
      word += line.slice(index + 1, end);
      index = end;
    } else if (character === '"') {
      for (index++; line[index] !== '"'; index++) {
        if (index >= line.length) return null;
        // Inside double quotes a backslash escapes only these; before anything else it is itself.
        const next = line[index + 1];
        const escaped = line[index] === "\\" && next !== undefined && '"\\$`'.includes(next);
        word += line[escaped ? ++index : index];
      }
    } else if (character === "\\") {
      if (index + 1 >= line.length) return null;
      word += line[++index];
    } else {
      word += character;
    }
  }
  if (word !== null) words.push(word);
  return words;
}
