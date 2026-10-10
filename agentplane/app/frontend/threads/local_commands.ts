import { equals, fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";

import { CommandSchema, type Command } from "../../../protocol/command_pb";
import { EventEntrySchema, type EventEntry } from "../../../protocol/event_log_pb";

export interface LocalCommand {
  command: Command;
  submittedAt: number;
  /** Persisted before an HTTP attempt; older records are conservatively treated as attempted. */
  attempted: boolean;
  /** HTTP admission evidence can be ahead of the consumed Event prefix. It never advances it. */
  admission: EventEntry | null;
}

export interface LocalCommandSnapshot {
  commands: LocalCommand[];
  /** Inputs whose admission is projected but whose referenced echo body has not arrived yet. */
  inputEchoes: Array<{ commandId: string; text: string }>;
  error: string | null;
}

export const MAX_RETAINED_COMMANDS = 128;

function decode(text: string): LocalCommand {
  const value = JSON.parse(text) as Record<string, unknown>;
  if (typeof value.submittedAt !== "number") {
    throw new Error("Invalid locally retained command submission time");
  }
  const command = fromJson(CommandSchema, value.command as JsonValue);
  if (!command.commandId || !command.operation.case) throw new Error("Invalid locally retained Command");
  const admission = value.admission === null ? null : fromJson(EventEntrySchema, value.admission as JsonValue);
  if (admission) checkAdmission(command, admission);
  if (value.attempted !== undefined && typeof value.attempted !== "boolean") {
    throw new Error("Invalid locally retained delivery state");
  }
  return { submittedAt: value.submittedAt, attempted: value.attempted ?? true, command, admission };
}

function encode(value: LocalCommand): string {
  return JSON.stringify({
    submittedAt: value.submittedAt,
    attempted: value.attempted,
    command: toJson(CommandSchema, value.command),
    admission: value.admission ? toJson(EventEntrySchema, value.admission) : null,
  });
}

export function checkAdmission(command: Command, admission: EventEntry): void {
  const observation = admission.event?.observation;
  if (
    admission.cursor < 1n ||
    !admission.origin?.sourceId ||
    admission.origin.sequence !== admission.cursor ||
    observation?.case !== "commandAdmitted" ||
    !observation.value.command ||
    !equals(CommandSchema, command, observation.value.command)
  ) {
    throw new Error(`Admission does not confirm command ${command.commandId}`);
  }
}

/** Browser recovery only: persistence here makes no server-side delivery promise. Each command
 * and pending input echo has its own key so simultaneous submissions in different tabs cannot
 * overwrite one another. The immutable Thread scope is also the HTTP submission target. */
export class LocalCommands {
  private readonly prefix: string;
  private readonly echoPrefix: string;
  private snapshot: LocalCommandSnapshot = { commands: [], inputEchoes: [], error: null };
  private readonly listeners = new Set<() => void>();

  constructor(readonly threadId: string) {
    this.prefix = `agentplane.pending:${encodeURIComponent(threadId)}:`;
    this.echoPrefix = `agentplane.input-echo:${encodeURIComponent(threadId)}:`;
    this.reload();
  }

  getSnapshot = (): LocalCommandSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    if (this.listeners.size === 1) {
      window.addEventListener("storage", this.onStorage);
      this.reload();
    }
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) window.removeEventListener("storage", this.onStorage);
    };
  };

  /** This must succeed before HTTP is attempted or the composer is cleared. */
  remember(command: Command): LocalCommand {
    if (this.snapshot.error) throw new Error(this.snapshot.error);
    const stored = localStorage.getItem(this.key(command.commandId));
    if (stored !== null) {
      const existing = decode(stored);
      if (!equals(CommandSchema, existing.command, command)) {
        throw new Error("A local command id cannot be reused with a different payload");
      }
      return existing;
    }
    if (this.snapshot.commands.length + this.snapshot.inputEchoes.length >= MAX_RETAINED_COMMANDS) {
      throw new Error(
        `Wait for retained commands to finish before submitting another (maximum ${MAX_RETAINED_COMMANDS})`
      );
    }
    const value: LocalCommand = { command, submittedAt: Date.now(), attempted: false, admission: null };
    localStorage.setItem(this.key(command.commandId), encode(value));
    this.reload();
    return value;
  }

  /** Record the attempt *before* sending: a lost response can never become a cancellable draft. */
  markAttempted(id: string): boolean {
    const key = this.key(id);
    const raw = localStorage.getItem(key);
    if (raw === null) return false;
    const value = decode(raw);
    if (value.admission) return false;
    if (!value.attempted) {
      localStorage.setItem(key, encode({ ...value, attempted: true }));
      this.reload();
    }
    return true;
  }

  /** Only a command with no HTTP attempt can be cancelled by this browser. */
  cancelUnsent(id: string): boolean {
    const key = this.key(id);
    const raw = localStorage.getItem(key);
    if (raw === null || decode(raw).attempted) return false;
    localStorage.removeItem(key);
    this.reload();
    return true;
  }

  acknowledge(command: Command, admission: EventEntry): void {
    checkAdmission(command, admission);
    this.checkKnownAdmission(command, admission);
    const stored = localStorage.getItem(this.key(command.commandId));
    // Replay may already have removed it while this HTTP request was outstanding. A late HTTP
    // completion must not resurrect a command the Event projection now owns.
    if (stored === null) return;
    const existing = decode(stored);
    if (!equals(CommandSchema, existing.command, command)) throw new Error("Local command payload changed");
    if (existing.admission && !equals(EventEntrySchema, existing.admission, admission)) {
      throw new Error("Conflicting command admission evidence");
    }
    // Admission is already a server fact even if caching that receipt fails. Keep it visible
    // in this page; the original persisted command and server replay still support reload.
    try {
      localStorage.setItem(this.key(command.commandId), encode({ ...existing, admission }));
    } catch (error) {
      this.snapshot = {
        commands: this.snapshot.commands.map((value) =>
          value.command.commandId === command.commandId ? { ...value, admission } : value
        ),
        inputEchoes: this.snapshot.inputEchoes,
        error: String(error),
      };
      for (const listener of this.listeners) listener();
      return;
    }
    this.reload();
  }

  /** A matching admission transfers command ownership to the Event fold; input text stays until echoed. */
  observePrefix(entries: readonly EventEntry[]): void {
    let changed = false;
    for (const entry of entries) {
      const observation = entry.event?.observation;
      if (observation?.case !== "commandAdmitted" || !observation.value.command) continue;
      const command = observation.value.command;
      const stored = localStorage.getItem(this.key(command.commandId));
      if (stored === null) continue;
      const existing = decode(stored);
      checkAdmission(existing.command, entry);
      this.checkKnownAdmission(existing.command, entry);
      if (existing.admission && !equals(EventEntrySchema, existing.admission, entry)) {
        throw new Error("Replayed admission conflicts with saved HTTP evidence");
      }
      if (!this.retainInputEcho(existing)) continue;
      localStorage.removeItem(this.key(command.commandId));
      changed = true;
    }
    if (changed) this.reload();
  }

  /** A projected command row takes delivery ownership; local input text stays until its body is fetched. */
  observeCommandIds(ids: ReadonlySet<string>): void {
    let changed = false;
    for (const id of ids) {
      const key = this.key(id);
      const stored = localStorage.getItem(key);
      if (stored === null) continue;
      let value: LocalCommand;
      try {
        value = decode(stored);
      } catch (error) {
        this.snapshot = { ...this.snapshot, error: String(error) };
        for (const listener of this.listeners) listener();
        continue;
      }
      if (!this.retainInputEcho(value)) continue;
      localStorage.removeItem(key);
      changed = true;
    }
    if (changed) this.reload();
  }

  /** The service echo has arrived; its body now replaces the locally retained submitted text. */
  acknowledgeEcho(id: string): void {
    localStorage.removeItem(this.echoKey(id));
    // The projected row itself confirms admission, so the original delivery record is no longer
    // needed either. This also prevents an immediate payload cache hit from copying it afterward.
    localStorage.removeItem(this.key(id));
    this.reload();
  }

  private key(id: string): string {
    return `${this.prefix}${encodeURIComponent(id)}`;
  }

  private echoKey(id: string): string {
    return `${this.echoPrefix}${encodeURIComponent(id)}`;
  }

  /** Preserve a locally sent input when its command record transfers to the server timeline. */
  private retainInputEcho(value: LocalCommand): boolean {
    const operation = value.command.operation;
    if (operation.case !== "submitInput") return true;
    const key = this.echoKey(value.command.commandId);
    try {
      const existing = localStorage.getItem(key);
      if (existing !== null) {
        const parsed = JSON.parse(existing) as { commandId?: unknown; text?: unknown };
        if (parsed.commandId !== value.command.commandId || parsed.text !== operation.value.text) {
          throw new Error(`Conflicting locally retained input echo for command ${value.command.commandId}`);
        }
      } else {
        localStorage.setItem(key, JSON.stringify({ commandId: value.command.commandId, text: operation.value.text }));
      }
      return true;
    } catch (error) {
      this.snapshot = { ...this.snapshot, error: String(error) };
      for (const listener of this.listeners) listener();
      return false;
    }
  }

  private checkKnownAdmission(command: Command, admission: EventEntry): void {
    const known = this.snapshot.commands.find((value) => value.command.commandId === command.commandId)?.admission;
    if (known && !equals(EventEntrySchema, known, admission)) {
      throw new Error("Conflicting command admission evidence");
    }
  }

  private onStorage = (event: StorageEvent): void => {
    if (event.key === null || event.key.startsWith(this.prefix) || event.key.startsWith(this.echoPrefix)) this.reload();
  };

  private reload(): void {
    try {
      const commands: LocalCommand[] = [];
      const inputEchoes: Array<{ commandId: string; text: string }> = [];
      const known = new Map(this.snapshot.commands.map((value) => [value.command.commandId, value]));
      for (let index = 0; index < localStorage.length; index++) {
        const key = localStorage.key(index);
        if (key?.startsWith(this.echoPrefix)) {
          const text = localStorage.getItem(key);
          if (text !== null) {
            const echo = JSON.parse(text) as { commandId?: unknown; text?: unknown };
            const commandId = decodeURIComponent(key.slice(this.echoPrefix.length));
            if (echo.commandId !== commandId || typeof echo.text !== "string") {
              throw new Error("Invalid locally retained input echo");
            }
            inputEchoes.push({ commandId, text: echo.text });
          }
          continue;
        }
        if (!key?.startsWith(this.prefix)) continue;
        const text = localStorage.getItem(key);
        if (text !== null) {
          const value = decode(text);
          const receipt = known.get(value.command.commandId)?.admission;
          if (receipt) {
            checkAdmission(value.command, receipt);
            if (value.admission && !equals(EventEntrySchema, value.admission, receipt)) {
              throw new Error("Stored command admission conflicts with observed evidence");
            }
            value.admission = receipt;
          }
          commands.push(value);
        }
      }
      commands.sort(
        (left, right) =>
          left.submittedAt - right.submittedAt || left.command.commandId.localeCompare(right.command.commandId)
      );
      inputEchoes.sort((left, right) => left.commandId.localeCompare(right.commandId));
      this.snapshot = { commands, inputEchoes, error: null };
    } catch (error) {
      // Leave both the stored bytes and the last readable view intact; do not silently discard
      // input because storage is unavailable or a local record cannot be decoded.
      this.snapshot = { ...this.snapshot, error: String(error) };
    }
    for (const listener of this.listeners) listener();
  }
}
