// Minimal logging front-end for the UI — not a framework, just namespaced levels over `console`.
// One choke point so every diagnostic has a consistent `[scope]` shape and there's a single place
// to later fan out to backend telemetry (see memory/improvements/ui-error-telemetry.md). House
// rule (STYLE.md): a catch that degrades gracefully still logs here — never a silent swallow.
//
// Usage, mirroring Python's `logger = logging.getLogger(__name__)`:
//   const log = logger("blob-cache");
//   log.warn("could not persist to localStorage", e);

type Level = "debug" | "info" | "warn" | "error";

export interface Logger {
  debug(message: string, ...detail: unknown[]): void;
  info(message: string, ...detail: unknown[]): void;
  warn(message: string, ...detail: unknown[]): void;
  error(message: string, ...detail: unknown[]): void;
}

function at(level: Level, scope: string) {
  return (message: string, ...detail: unknown[]): void => console[level](`[${scope}] ${message}`, ...detail);
}

export function logger(scope: string): Logger {
  return { debug: at("debug", scope), info: at("info", scope), warn: at("warn", scope), error: at("error", scope) };
}
