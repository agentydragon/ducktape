"""Persisted enum values used by the Dispatcher migration metadata."""

from enum import StrEnum


class Operation(StrEnum):
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
