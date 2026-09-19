import { equals, fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";

import { CommandSchema, type Command } from "../../protocol/command_pb";
import { EventEntrySchema, type EventEntry } from "../../protocol/event_log_pb";

export interface LocalCommand {
  command: Command;
  submittedAt: number;
  /** HTTP admission evidence can be ahead of the consumed Event prefix. It never advances it. */
  admission: EventEntry | null;
}

export interface LocalCommandSnapshot {
  commands: LocalCommand[];
  error: string | null;
}

function decode(text: string): LocalCommand {
  const value = JSON.parse(text) as Record<string, unknown>;
  if (typeof value.submittedAt !== "number") {
    throw new Error("Invalid locally retained command submission time");
  }
  const command = fromJson(CommandSchema, value.command as JsonValue);
  if (!command.commandId || !command.operation.case) throw new Error("Invalid locally retained Command");
  const admission = value.admission === null ? null : fromJson(EventEntrySchema, value.admission as JsonValue);
  if (admission) checkAdmission(command, admission);
  return { submittedAt: value.submittedAt, command, admission };
}

function encode(value: LocalCommand): string {
  return JSON.stringify({
    submittedAt: value.submittedAt,
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
 * has its own key so simultaneous submissions in different tabs cannot overwrite one another.
 * The immutable Thread scope is also the HTTP submission target. */
export class LocalCommands {
  private readonly prefix: string;
  private snapshot: LocalCommandSnapshot = { commands: [], error: null };
  private readonly listeners = new Set<() => void>();

  constructor(readonly threadId: string) {
    this.prefix = `agentplane.pending:${encodeURIComponent(threadId)}:`;
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
    const value: LocalCommand = { command, submittedAt: Date.now(), admission: null };
    localStorage.setItem(this.key(command.commandId), encode(value));
    this.reload();
    return value;
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
        error: String(error),
      };
      for (const listener of this.listeners) listener();
      return;
    }
    this.reload();
  }

  /** Only a matching admission in the verified prefix transfers ownership to the Event fold. */
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
      localStorage.removeItem(this.key(command.commandId));
      changed = true;
    }
    if (changed) this.reload();
  }

  private key(id: string): string {
    return `${this.prefix}${encodeURIComponent(id)}`;
  }

  private checkKnownAdmission(command: Command, admission: EventEntry): void {
    const known = this.snapshot.commands.find((value) => value.command.commandId === command.commandId)?.admission;
    if (known && !equals(EventEntrySchema, known, admission)) {
      throw new Error("Conflicting command admission evidence");
    }
  }

  private onStorage = (event: StorageEvent): void => {
    if (event.key === null || event.key.startsWith(this.prefix)) this.reload();
  };

  private reload(): void {
    try {
      const commands: LocalCommand[] = [];
      const known = new Map(this.snapshot.commands.map((value) => [value.command.commandId, value]));
      for (let index = 0; index < localStorage.length; index++) {
        const key = localStorage.key(index);
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
      this.snapshot = { commands, error: null };
    } catch (error) {
      // Leave both the stored bytes and the last readable view intact; do not silently discard
      // input because storage is unavailable or a local record cannot be decoded.
      this.snapshot = { ...this.snapshot, error: String(error) };
    }
    for (const listener of this.listeners) listener();
  }
}
