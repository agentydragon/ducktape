import type { Command } from "../../protocol/command_pb";
import type { EventEntry } from "../../protocol/event_log_pb";

export interface ObservedCommand {
  command: Command;
  admission: EventEntry;
  outcome: EventEntry | null;
}

/** The runner's admitted queue, not an app/browser submission queue. Keep the actual evidence
 * instead of translating receipt states into a second protocol. Uncorrelated native observations
 * remain in the raw log; they cannot settle an unrelated command. */
export function observedCommands(entries: readonly EventEntry[]): ObservedCommand[] {
  const commands = new Map<string, ObservedCommand>();
  for (const entry of entries) {
    const observation = entry.event?.observation;
    let settled: readonly string[] = [];
    switch (observation?.case) {
      case "commandAdmitted": {
        const command = observation.value.command;
        if (command) commands.set(command.commandId, { command, admission: entry, outcome: null });
        break;
      }
      case "harnessUserMessageConfirmed":
        settled = observation.value.originCommandIds;
        break;
      case "modelChanged":
      case "commandFailed":
      case "commandNoop":
        settled = [observation.value.commandId];
        break;
      case "turnCompleted":
        settled = [observation.value.interruptedByCommandId];
        break;
      case "harnessExited":
        settled = [observation.value.stoppedByCommandId];
        break;
    }
    for (const id of settled) {
      const command = commands.get(id);
      if (command) commands.set(id, { ...command, outcome: entry });
    }
  }
  return [...commands.values()];
}

export function commandLabel(command: Command): string {
  switch (command.operation.case) {
    case "submitInput":
      return "Message";
    case "changeModel":
      return `Change model to ${command.operation.value.model}`;
    case "interruptTurn":
      return `Interrupt turn ${command.operation.value.turnId}`;
    case "stopRunnerSession":
      return "Stop harness";
    default:
      return "Command";
  }
}
