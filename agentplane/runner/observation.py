"""Harness-neutral observations carried by public Events."""

from typing import assert_never

from agentplane.proto import event_pb2

Observation = (
    event_pb2.HarnessStarted
    | event_pb2.HarnessExited
    | event_pb2.HarnessLost
    | event_pb2.HarnessStderr
    | event_pb2.HarnessLaunchFailed
    | event_pb2.SetupStarted
    | event_pb2.SetupOutput
    | event_pb2.SetupFinished
    | event_pb2.SetupInterrupted
    | event_pb2.CommandAdmitted
    | event_pb2.CommandFailed
    | event_pb2.CommandNoop
    | event_pb2.HarnessUserMessageConfirmed
    | event_pb2.ModelChanged
    | event_pb2.ReasoningEffortChanged
    | event_pb2.TurnStarted
    | event_pb2.TurnCompleted
    | event_pb2.ItemStarted
    | event_pb2.TextDelta
    | event_pb2.ToolArgumentsDelta
    | event_pb2.ToolArguments
    | event_pb2.ToolOutputDelta
    | event_pb2.ItemCompleted
    | event_pb2.Native
    | event_pb2.ConversationReconciled
    | event_pb2.DebugCheckpoint
)


def observation_event(observation: Observation) -> event_pb2.Event:
    match observation:
        case event_pb2.HarnessStarted():
            return event_pb2.Event(harness_started=observation)
        case event_pb2.HarnessExited():
            return event_pb2.Event(harness_exited=observation)
        case event_pb2.HarnessLost():
            return event_pb2.Event(harness_lost=observation)
        case event_pb2.HarnessStderr():
            return event_pb2.Event(harness_stderr=observation)
        case event_pb2.HarnessLaunchFailed():
            return event_pb2.Event(harness_launch_failed=observation)
        case event_pb2.SetupStarted():
            return event_pb2.Event(setup_started=observation)
        case event_pb2.SetupOutput():
            return event_pb2.Event(setup_output=observation)
        case event_pb2.SetupFinished():
            return event_pb2.Event(setup_finished=observation)
        case event_pb2.SetupInterrupted():
            return event_pb2.Event(setup_interrupted=observation)
        case event_pb2.CommandAdmitted():
            return event_pb2.Event(command_admitted=observation)
        case event_pb2.CommandFailed():
            return event_pb2.Event(command_failed=observation)
        case event_pb2.CommandNoop():
            return event_pb2.Event(command_noop=observation)
        case event_pb2.HarnessUserMessageConfirmed():
            return event_pb2.Event(harness_user_message_confirmed=observation)
        case event_pb2.ModelChanged():
            return event_pb2.Event(model_changed=observation)
        case event_pb2.ReasoningEffortChanged():
            return event_pb2.Event(reasoning_effort_changed=observation)
        case event_pb2.TurnStarted():
            return event_pb2.Event(turn_started=observation)
        case event_pb2.TurnCompleted():
            return event_pb2.Event(turn_completed=observation)
        case event_pb2.ItemStarted():
            return event_pb2.Event(item_started=observation)
        case event_pb2.TextDelta():
            return event_pb2.Event(text_delta=observation)
        case event_pb2.ToolArgumentsDelta():
            return event_pb2.Event(tool_arguments_delta=observation)
        case event_pb2.ToolArguments():
            return event_pb2.Event(tool_arguments=observation)
        case event_pb2.ToolOutputDelta():
            return event_pb2.Event(tool_output_delta=observation)
        case event_pb2.ItemCompleted():
            return event_pb2.Event(item_completed=observation)
        case event_pb2.Native():
            return event_pb2.Event(native=observation)
        case event_pb2.ConversationReconciled():
            return event_pb2.Event(conversation_reconciled=observation)
        case event_pb2.DebugCheckpoint():
            return event_pb2.Event(debug_checkpoint=observation)
        case _:
            assert_never(observation)
