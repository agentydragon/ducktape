from google.api import annotations_pb2

from agentplane.app import app_service_pb2
from agentplane.proto import environment_lifecycle_pb2


def test_generated_bindings_include_google_protos() -> None:
    method = app_service_pb2.DESCRIPTOR.services_by_name["AppService"].methods_by_name["GetSystemStatus"]
    # Generated stubs type extensions as FieldDescriptor rather than _ExtensionFieldDescriptor.
    assert method.GetOptions().Extensions[annotations_pb2.http].get == "/v1/system/status"  # type: ignore[index]

    assessment = environment_lifecycle_pb2.WakeAssessment()
    assessment.error.code = 7
    assert assessment.error.code == 7


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
