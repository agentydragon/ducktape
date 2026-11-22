import { create, equals, fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";

import { CommandSchema, type Command } from "../../../proto/command_pb";
import { EventEntrySchema, type EventEntry } from "../../../proto/event_log_pb";
import {
  DispatchSchema,
  type Dispatch,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { dispatchFromJson, normalizeDeliveryState, savedInterruptResultFromJson } from "./client";

/** A typed operation retained for lost-response recovery; Command protobuf belongs to the journal. */
type PlainOperation =
  | { case: "submitInput"; value: { text: string } }
  | { case: "changeModel"; value: { model: string } }
  | { case: "changeReasoningEffort"; value: { effort: string } }
  | { case: "interruptTurn"; value: { turnId: string } };

export type PendingCommand = {
  commandId: string;
  operation: PlainOperation | Command["operation"];
};

function sameCommand(left: PendingCommand, right: PendingCommand): boolean {
  return left.commandId === right.commandId && JSON.stringify(left.operation) === JSON.stringify(right.operation);
}

type SandboxDestination = { sandbox: string; sandboxUid: string };

export interface LocalCommand {
  command: PendingCommand;
  /** The original Sandbox destination, when saved, fences legacy receipt reconciliation. */
  destination: { kind: "session" } | { kind: "reconcile-only"; target: SandboxDestination | null };
  submittedAt: number;
  /** Persisted before an HTTP attempt; older records are conservatively treated as attempted. */
  attempted: boolean;
  /** HTTP admission evidence can be ahead of the consumed Event prefix. It never advances it. */
  admission: EventEntry | null;
  /** Dispatcher state is durable queue evidence, not proof of a harness effect. */
  dispatch: Dispatch | null;
  /** Previously stored immediate interrupt evidence prevents replay of an old command. */
  interruptResult?: Dispatch | null;
}

export interface LocalCommandSnapshot {
  commands: LocalCommand[];
  /** Inputs whose admission is projected but whose referenced echo body has not arrived yet. */
  inputEchoes: Array<{ commandId: string; text: string }>;
  error: string | null;
  /** Transient interruption of updates for commands still queued or delivering. */
  dispatchWatchError: string | null;
  /** Sticky warning that the Dispatcher could no longer prove deletion coverage for this Session. */
  dispatchWatchCoverageLost: boolean;
}

export const MAX_RETAINED_COMMANDS = 128;

export function canonicalDispatchCommandIds(commandIds: readonly string[]): string[] {
  return [...new Set(commandIds)].sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));
}

function parseRecord(text: string): Record<string, unknown> {
  return JSON.parse(text) as Record<string, unknown>;
}

function decode(value: Record<string, unknown>): LocalCommand {
  if (typeof value.submittedAt !== "number") {
    throw new Error("Invalid locally retained command submission time");
  }
  const saved = value.command as Record<string, unknown>;
  // Preserve locally authored, unconfirmed inputs saved by the previous protobuf-JSON format.
  const protoJson = saved.operation === undefined;
  const command: PendingCommand = protoJson ? fromJson(CommandSchema, saved as JsonValue) : (saved as PendingCommand);
  if (!command.commandId || !command.operation.case) throw new Error("Invalid locally retained Command");
  const target = value.target ?? null;
  if (
    target !== null &&
    (typeof target !== "object" ||
      Array.isArray(target) ||
      typeof (target as Record<string, unknown>).sandbox !== "string" ||
      typeof (target as Record<string, unknown>).sandboxUid !== "string" ||
      !(target as SandboxDestination).sandbox ||
      !(target as SandboxDestination).sandboxUid)
  ) {
    throw new Error("Invalid locally retained Sandbox destination");
  }
  const admission = value.admission === null ? null : fromJson(EventEntrySchema, value.admission as JsonValue);
  if (admission) checkAdmission(command, admission);
  let dispatch: Dispatch | null = null;
  if (value.dispatch !== undefined && value.dispatch !== null) {
    if (typeof value.dispatch !== "object" || Array.isArray(value.dispatch)) {
      throw new Error("Invalid locally retained Dispatcher state");
    }
    dispatch = dispatchFromJson(value.dispatch);
    if (dispatch.commandId !== command.commandId || !dispatch.sessionId) {
      throw new Error("Invalid locally retained Dispatcher state");
    }
    if (dispatch.admission) checkAdmission(command, dispatch.admission);
  }
  if (value.attempted !== undefined && typeof value.attempted !== "boolean") {
    throw new Error("Invalid locally retained delivery state");
  }
  let interruptResult: Dispatch | null = null;
  if (value.interruptResult !== undefined && value.interruptResult !== null) {
    if (
      command.operation.case !== "interruptTurn" ||
      typeof value.interruptResult !== "object" ||
      Array.isArray(value.interruptResult)
    ) {
      throw new Error("Invalid locally retained interrupt result");
    }
    interruptResult = savedInterruptResultFromJson(value.interruptResult);
    if (interruptResult.admission) checkAdmission(command, interruptResult.admission);
  }
  return {
    submittedAt: value.submittedAt,
    attempted: value.attempted ?? true,
    command,
    destination:
      value.sessionAddressed === true && target === null && !protoJson
        ? { kind: "session" }
        : { kind: "reconcile-only", target: target as SandboxDestination | null },
    admission,
    dispatch,
    interruptResult,
  };
}

function checkAdmission(command: PendingCommand, admission: EventEntry): void {
  const observation = admission.event?.observation;
  if (
    admission.cursor < 1n ||
    !admission.origin?.sourceId ||
    admission.origin.sequence !== admission.cursor ||
    observation?.case !== "commandAdmitted" ||
    !observation.value.command ||
    !equals(CommandSchema, create(CommandSchema, command), observation.value.command)
  ) {
    throw new Error(`Admission does not confirm command ${command.commandId}`);
  }
}

/** Browser recovery only: persistence here makes no server-side delivery promise. Each command
 * and pending input echo has its own key so simultaneous submissions in different tabs cannot
 * overwrite one another. Keys scope current commands to their Session. */
export class LocalCommands {
  private readonly prefix: string;
  private readonly echoPrefix: string;
  private readonly dispatchWatchPrefix: string;
  private readonly coverageLostKey: string;
  private snapshot: LocalCommandSnapshot = {
    commands: [],
    inputEchoes: [],
    error: null,
    dispatchWatchError: null,
    dispatchWatchCoverageLost: false,
  };
  private readonly listeners = new Set<() => void>();

  constructor(readonly threadId: string) {
    this.prefix = `agentplane.pending:${encodeURIComponent(threadId)}:`;
    this.echoPrefix = `agentplane.input-echo:${encodeURIComponent(threadId)}:`;
    this.dispatchWatchPrefix = `agentplane.dispatch-watch:${encodeURIComponent(threadId)}:`;
    this.coverageLostKey = `agentplane.dispatch-watch-coverage-lost:${encodeURIComponent(threadId)}`;
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
  remember(command: PendingCommand): LocalCommand {
    if (this.snapshot.error) throw new Error(this.snapshot.error);
    const stored = localStorage.getItem(this.key(command.commandId));
    if (stored !== null) {
      const existing = decode(parseRecord(stored));
      if (!sameCommand(existing.command, command)) {
        throw new Error("A local command id cannot be reused with a different payload");
      }
      return existing;
    }
    if (this.snapshot.commands.length + this.snapshot.inputEchoes.length >= MAX_RETAINED_COMMANDS) {
      throw new Error(
        `Wait for retained commands to finish before submitting another (maximum ${MAX_RETAINED_COMMANDS})`
      );
    }
    const value = {
      command,
      submittedAt: Date.now(),
      attempted: false,
      admission: null,
      dispatch: null,
      interruptResult: null,
    };
    localStorage.setItem(
      this.key(command.commandId),
      JSON.stringify({ ...value, sessionAddressed: true, target: null })
    );
    this.reload();
    return { ...value, destination: { kind: "session" } };
  }

  /** Record the attempt *before* sending: a lost response can never become a cancellable draft. */
  markAttempted(id: string): boolean {
    const key = this.key(id);
    const raw = localStorage.getItem(key);
    if (raw === null) return false;
    const record = parseRecord(raw);
    const value = decode(record);
    if (value.destination.kind === "reconcile-only" || value.admission || value.dispatch || value.interruptResult) {
      return false;
    }
    if (!value.attempted) {
      localStorage.setItem(key, JSON.stringify({ ...record, attempted: true }));
      this.reload();
    }
    return true;
  }

  /** Only a command with no HTTP attempt can be cancelled by this browser. */
  cancelUnsent(id: string): boolean {
    const key = this.key(id);
    const raw = localStorage.getItem(key);
    if (raw === null) return false;
    const value = decode(parseRecord(raw));
    if (value.destination.kind === "reconcile-only" || value.attempted) return false;
    localStorage.removeItem(key);
    this.reload();
    return true;
  }

  acknowledgeDispatch(command: PendingCommand, dispatch: Dispatch): boolean {
    if (dispatch.commandId !== command.commandId) throw new Error("Dispatcher receipt has a different command id");
    const normalizedDispatch = { ...dispatch, state: normalizeDeliveryState(dispatch.state) };
    if (dispatch.admission) {
      checkAdmission(command, dispatch.admission);
      this.checkKnownAdmission(command, dispatch.admission);
    }
    const stored = localStorage.getItem(this.key(command.commandId));
    // A replayed server row may already own the command while an HTTP status read is in flight.
    if (stored === null) return true;
    const record = parseRecord(stored);
    const existing = decode(record);
    if (!sameCommand(existing.command, command)) throw new Error("Local command payload changed");
    const target = existing.destination.kind === "reconcile-only" ? existing.destination.target : null;
    const hasSandbox = normalizedDispatch.sandbox !== "";
    const hasSandboxUid = normalizedDispatch.sandboxUid !== "";
    if (
      normalizedDispatch.sessionId !== this.threadId ||
      hasSandbox !== hasSandboxUid ||
      (target &&
        hasSandbox &&
        (normalizedDispatch.sandbox !== target.sandbox || normalizedDispatch.sandboxUid !== target.sandboxUid))
    ) {
      throw new Error("Dispatcher receipt has a different Sandbox destination");
    }
    const previousUpdate = existing.dispatch?.updatedAt;
    const nextUpdate = normalizedDispatch.updatedAt;
    if (
      previousUpdate &&
      nextUpdate &&
      (nextUpdate.seconds < previousUpdate.seconds ||
        (nextUpdate.seconds === previousUpdate.seconds && nextUpdate.nanos < previousUpdate.nanos))
    ) {
      return true;
    }
    const admission = normalizedDispatch.admission ?? existing.admission;
    try {
      localStorage.setItem(
        this.key(command.commandId),
        // Other tabs still read the authored command and destination in their original format.
        JSON.stringify({
          ...record,
          admission: admission ? toJson(EventEntrySchema, admission) : null,
          dispatch: toJson(DispatchSchema, normalizedDispatch),
        })
      );
    } catch (error) {
      this.snapshot = {
        ...this.snapshot,
        commands: this.snapshot.commands.map((value) =>
          value.command.commandId === command.commandId ? { ...value, admission, dispatch: normalizedDispatch } : value
        ),
        inputEchoes: this.snapshot.inputEchoes,
        error: String(error),
      };
      for (const listener of this.listeners) listener();
      return false;
    }
    this.reload();
    return true;
  }

  updateDispatch(commandId: string, dispatch: Dispatch): boolean {
    const stored = localStorage.getItem(this.key(commandId));
    if (stored === null) return true;
    return this.acknowledgeDispatch(decode(parseRecord(stored)).command, dispatch);
  }

  /** A cursor is stored per effective Session and canonical command filter. */
  dispatchWatchResumeToken(sessionId: string, commandIds: readonly string[]): string | null {
    const key = this.dispatchWatchCursorKey(sessionId);
    try {
      const saved = this.readDispatchWatchCursor(key);
      return saved && this.sameDispatchWatchFilter(saved.commandIds, commandIds) ? saved.token : null;
    } catch (error) {
      this.reportStorageFailure(error);
      return null;
    }
  }

  /** Call only after every change in the frame has reached durable LocalCommands state. */
  saveDispatchWatchResumeToken(sessionId: string, commandIds: readonly string[], token: string): boolean {
    if (token === "") throw new Error("Dispatcher watch frame omitted its resume token");
    const key = this.dispatchWatchCursorKey(sessionId);
    try {
      localStorage.setItem(key, JSON.stringify({ commandIds: canonicalDispatchCommandIds(commandIds), token }));
      return true;
    } catch (error) {
      this.reportStorageFailure(error);
      return false;
    }
  }

  clearDispatchWatchResumeToken(sessionId: string, commandIds: readonly string[]): boolean {
    const key = this.dispatchWatchCursorKey(sessionId);
    try {
      const saved = this.readDispatchWatchCursor(key);
      if (saved && this.sameDispatchWatchFilter(saved.commandIds, commandIds)) localStorage.removeItem(key);
      return true;
    } catch (error) {
      this.reportStorageFailure(error);
      return false;
    }
  }

  setDispatchWatchError(error: string | null): void {
    if (this.snapshot.dispatchWatchError === error) return;
    this.snapshot = { ...this.snapshot, dispatchWatchError: error };
    for (const listener of this.listeners) listener();
  }

  /** Deletion coverage cannot be recovered after its retention window; keep the warning across reloads. */
  recordDispatchWatchCoverageLoss(): void {
    this.snapshot = { ...this.snapshot, dispatchWatchCoverageLost: true };
    for (const listener of this.listeners) listener();
    try {
      localStorage.setItem(this.coverageLostKey, "true");
    } catch (error) {
      this.reportStorageFailure(error);
    }
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
        value = decode(parseRecord(stored));
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

  private dispatchWatchCursorKey(sessionId: string): string {
    if (sessionId !== this.threadId) throw new Error("Dispatcher watch belongs to a different Session");
    return `${this.dispatchWatchPrefix}cursor`;
  }

  private readDispatchWatchCursor(key: string): { commandIds: string[]; token: string } | null {
    const text = localStorage.getItem(key);
    if (text === null) return null;
    const value = JSON.parse(text) as Record<string, unknown>;
    if (
      typeof value !== "object" ||
      value === null ||
      !Array.isArray(value.commandIds) ||
      value.commandIds.some((id) => typeof id !== "string") ||
      typeof value.token !== "string" ||
      value.token === ""
    ) {
      throw new Error("Invalid locally retained Dispatcher watch cursor");
    }
    const commandIds = value.commandIds as string[];
    const canonicalIds = canonicalDispatchCommandIds(commandIds);
    if (canonicalIds.length !== commandIds.length || canonicalIds.some((id, index) => id !== commandIds[index])) {
      throw new Error("Invalid locally retained Dispatcher watch filter");
    }
    return { commandIds: canonicalIds, token: value.token };
  }

  private sameDispatchWatchFilter(left: readonly string[], right: readonly string[]): boolean {
    const canonicalRight = canonicalDispatchCommandIds(right);
    return left.length === canonicalRight.length && left.every((id, index) => id === canonicalRight[index]);
  }

  private reportStorageFailure(error: unknown): void {
    this.snapshot = { ...this.snapshot, error: String(error) };
    for (const listener of this.listeners) listener();
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

  private checkKnownAdmission(command: PendingCommand, admission: EventEntry): void {
    const known = this.snapshot.commands.find((value) => value.command.commandId === command.commandId)?.admission;
    if (known && !equals(EventEntrySchema, known, admission)) {
      throw new Error("Conflicting command admission evidence");
    }
  }

  private onStorage = (event: StorageEvent): void => {
    if (
      event.key === null ||
      event.key.startsWith(this.prefix) ||
      event.key.startsWith(this.echoPrefix) ||
      event.key.startsWith(this.dispatchWatchPrefix) ||
      event.key === this.coverageLostKey
    )
      this.reload();
  };

  private reload(): void {
    try {
      const commands: LocalCommand[] = [];
      const inputEchoes: Array<{ commandId: string; text: string }> = [];
      const dispatchWatchCoverageLost =
        this.snapshot.dispatchWatchCoverageLost || localStorage.getItem(this.coverageLostKey) !== null;
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
          const value = decode(parseRecord(text));
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
      this.snapshot = { ...this.snapshot, commands, inputEchoes, error: null, dispatchWatchCoverageLost };
    } catch (error) {
      // Leave both the stored bytes and the last readable view intact; do not silently discard
      // input because storage is unavailable or a local record cannot be decoded.
      this.snapshot = { ...this.snapshot, error: String(error) };
    }
    for (const listener of this.listeners) listener();
  }
}
