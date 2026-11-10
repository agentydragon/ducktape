from google.rpc import status_pb2

from devinfra.python.proto_integration import status_envelope_pb2


def make_status(code: int, message: str) -> status_pb2.Status:
    envelope = status_envelope_pb2.StatusEnvelope(status=status_pb2.Status(code=code, message=message))
    status: status_pb2.Status = envelope.status
    return status
