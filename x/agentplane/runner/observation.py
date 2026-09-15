"""Harness-neutral observations carried by public Events."""

from google.protobuf.message import Message

from x.agentplane.protocol import event_pb2

Observation = (
    event_pb2.HarnessStarted
    | event_pb2.HarnessExited
    | event_pb2.HarnessLost
    | event_pb2.HarnessStderr
    | event_pb2.CommandAdmitted
    | event_pb2.CommandFailed
    | event_pb2.CommandNoop
    | event_pb2.HarnessUserMessageConfirmed
    | event_pb2.ModelChanged
    | event_pb2.TurnStarted
    | event_pb2.TurnCompleted
    | event_pb2.ItemStarted
    | event_pb2.TextDelta
    | event_pb2.ToolArgumentsDelta
    | event_pb2.ToolArguments
    | event_pb2.ToolOutputDelta
    | event_pb2.ItemCompleted
    | event_pb2.Native
    | event_pb2.DebugCheckpoint
)

_FIELDS: dict[type[Message], str] = {
    event_pb2.HarnessStarted: "harness_started",
    event_pb2.HarnessExited: "harness_exited",
    event_pb2.HarnessLost: "harness_lost",
    event_pb2.HarnessStderr: "harness_stderr",
    event_pb2.CommandAdmitted: "command_admitted",
    event_pb2.CommandFailed: "command_failed",
    event_pb2.CommandNoop: "command_noop",
    event_pb2.HarnessUserMessageConfirmed: "harness_user_message_confirmed",
    event_pb2.ModelChanged: "model_changed",
    event_pb2.TurnStarted: "turn_started",
    event_pb2.TurnCompleted: "turn_completed",
    event_pb2.ItemStarted: "item_started",
    event_pb2.TextDelta: "text_delta",
    event_pb2.ToolArgumentsDelta: "tool_arguments_delta",
    event_pb2.ToolArguments: "tool_arguments",
    event_pb2.ToolOutputDelta: "tool_output_delta",
    event_pb2.ItemCompleted: "item_completed",
    event_pb2.Native: "native",
    event_pb2.DebugCheckpoint: "debug_checkpoint",
}


def observation_event(observation: Observation) -> event_pb2.Event:
    event = event_pb2.Event()
    getattr(event, _FIELDS[type(observation)]).CopyFrom(observation)
    return event
