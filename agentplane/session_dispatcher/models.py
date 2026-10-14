"""The Session Dispatcher's delivery vocabulary: what a Dispatch is and where it is."""

from enum import StrEnum

from agentplane.proto import command_pb2

# gazelle:include_dep @pypi//protobuf


class Operation(StrEnum):
    """What a stored Dispatch asks of a Session. `InterruptTurn` is never stored."""

    SUBMIT_INPUT = "submit_input"
    CHANGE_MODEL = "change_model"
    CHANGE_REASONING_EFFORT = "change_reasoning_effort"
    STOP_RUNNER_SESSION = "stop_runner_session"


class DeliveryState(StrEnum):
    QUEUED_IN_DISPATCHER = "QUEUED_IN_DISPATCHER"
    DELIVERING_TO_RUNNER = "DELIVERING_TO_RUNNER"
    RUNNER_ADMITTED = "RUNNER_ADMITTED"
    HARNESS_CONFIRMED = "HARNESS_CONFIRMED"
    NO_OP = "NO_OP"
    FAILED = "FAILED"
    CANCELLED_IN_DISPATCHER = "CANCELLED_IN_DISPATCHER"
    UNKNOWN = "UNKNOWN"


# States a delivery can still change: the Sandbox's deletion ends them, a cancel or a stop may too.
LIVE_STATES = frozenset({DeliveryState.QUEUED_IN_DISPATCHER, DeliveryState.DELIVERING_TO_RUNNER})


class Reason(StrEnum):
    """Why a Dispatch waits or ended; a runner's refusal reads `runner <CODE>: <details>` instead."""

    SANDBOX_DELETED = "sandbox_deleted"
    SANDBOX_SUSPENDED = "sandbox_suspended"
    SANDBOX_RESUMING = "sandbox_resuming"
    SANDBOX_NOT_READY = "sandbox_not_ready"
    WAKE_ON_SEND_DISABLED = "wake_on_send_disabled"
    WAKE_CALLER_UNKNOWN = "wake_caller_unknown"
    WAKE_RATE_LIMITED = "wake_rate_limited"
    RUNNER_REFUSED_WORK = "runner_refused_work"
    DELIVERY_UNAVAILABLE = "delivery_unavailable"
    DISPATCHER_RESTARTED = "dispatcher_restarted"
    NOTHING_RUNNING = "nothing_running"
    STOPPED = "stopped"
    SENDER = "sender"


def operation_of(command: command_pb2.Command) -> Operation:
    match command.WhichOneof("operation"):
        case "submit_input":
            return Operation.SUBMIT_INPUT
        case "change_model":
            return Operation.CHANGE_MODEL
        case "change_reasoning_effort":
            return Operation.CHANGE_REASONING_EFFORT
        case "stop_runner_session":
            return Operation.STOP_RUNNER_SESSION
        case other:
            raise ValueError(f"{other} is not a stored operation")
