"""The exact wire snapshots shared by Dispatch reads and terminal watches."""

# gazelle:include_dep @pypi//protobuf

from agentplane.proto import session_dispatcher_pb2
from agentplane.session_dispatcher.db import Dispatch, SessionOpening
from agentplane.session_dispatcher.models import DeliveryState, Operation

_OPERATIONS = {
    Operation.SUBMIT_INPUT: session_dispatcher_pb2.OPERATION_SUBMIT_INPUT,
    Operation.CHANGE_MODEL: session_dispatcher_pb2.OPERATION_CHANGE_MODEL,
    Operation.CHANGE_REASONING_EFFORT: session_dispatcher_pb2.OPERATION_CHANGE_REASONING_EFFORT,
    Operation.STOP_RUNNER_SESSION: session_dispatcher_pb2.OPERATION_STOP_RUNNER_SESSION,
}
_STATES = {
    DeliveryState.QUEUED_IN_DISPATCHER: session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
    DeliveryState.DELIVERING_TO_RUNNER: session_dispatcher_pb2.DELIVERY_STATE_DELIVERING_TO_RUNNER,
    DeliveryState.RUNNER_ADMITTED: session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED,
    DeliveryState.HARNESS_CONFIRMED: session_dispatcher_pb2.DELIVERY_STATE_HARNESS_CONFIRMED,
    DeliveryState.NO_OP: session_dispatcher_pb2.DELIVERY_STATE_NO_OP,
    DeliveryState.FAILED: session_dispatcher_pb2.DELIVERY_STATE_FAILED,
    DeliveryState.CANCELLED_IN_DISPATCHER: session_dispatcher_pb2.DELIVERY_STATE_CANCELLED_IN_DISPATCHER,
    DeliveryState.UNKNOWN: session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN,
}


def describe_state(state: DeliveryState) -> int:
    return _STATES[state]


def describe_dispatch(row: Dispatch) -> session_dispatcher_pb2.Dispatch:
    dispatch = session_dispatcher_pb2.Dispatch(
        command_id=row.command_id,
        session_id=row.session_id,
        sandbox=row.sandbox_name,
        sandbox_uid=str(row.sandbox_uid),
        operation=_OPERATIONS[row.operation],
        state=_STATES[row.state],
        reason=row.reason,
    )
    if row.admission is not None:
        dispatch.admission.CopyFrom(row.admission)
    dispatch.created_at.FromDatetime(row.created_at)
    dispatch.updated_at.FromDatetime(row.updated_at)
    return dispatch


def describe_opening(row: SessionOpening) -> session_dispatcher_pb2.SessionOpening:
    opening = session_dispatcher_pb2.SessionOpening(
        session_id=row.session_id,
        sandbox=row.sandbox_name,
        sandbox_uid=str(row.sandbox_uid),
        state=_STATES[row.state],
        reason=row.reason,
    )
    if row.attached is not None:
        opening.attached.CopyFrom(row.attached)
    opening.created_at.FromDatetime(row.created_at)
    opening.updated_at.FromDatetime(row.updated_at)
    return opening
