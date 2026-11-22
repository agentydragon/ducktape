import { Operation, type Command } from "../../../proto/command_pb";
import { DeliveryState } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";

export function deliveryOperationLabel(operation: Operation): string {
  switch (operation) {
    case Operation.SUBMIT_INPUT:
      return "Send message";
    case Operation.CHANGE_MODEL:
      return "Model change";
    case Operation.CHANGE_REASONING_EFFORT:
      return "Reasoning effort change";
    case Operation.INTERRUPT_TURN:
      return "Turn interrupt";
    case Operation.COMPACT:
      return "Compact conversation";
    default:
      return "Command";
  }
}

export const deliveryStateLabel: Record<DeliveryState, string> = {
  [DeliveryState.UNSPECIFIED]: "Delivery unconfirmed",
  [DeliveryState.QUEUED_IN_DISPATCHER]: "Queued in Dispatcher",
  [DeliveryState.DELIVERING_TO_RUNNER]: "Sending to runner",
  [DeliveryState.RUNNER_ADMITTED]: "Runner accepted",
  [DeliveryState.FAILED]: "Delivery failed",
  [DeliveryState.CANCELLED_IN_DISPATCHER]: "Cancelled before delivery",
  [DeliveryState.UNKNOWN]: "Outcome unknown",
};

export function storedCommandLabel(operation: Operation, command?: Command): string {
  const original = command?.operation;
  if (operation === Operation.SUBMIT_INPUT && original?.case === "submitInput") {
    const preview = original.value.text.trim().replace(/\s+/g, " ");
    if (!preview) return "Empty message";
    const characters = Array.from(preview);
    return characters.length > 88 ? characters.slice(0, 88).join("") + "…" : preview;
  }
  if (operation === Operation.CHANGE_MODEL && original?.case === "changeModel") {
    return "Change model to " + original.value.model;
  }
  if (operation === Operation.CHANGE_REASONING_EFFORT && original?.case === "changeReasoningEffort") {
    return "Set reasoning effort to " + original.value.effort;
  }
  return deliveryOperationLabel(operation);
}
