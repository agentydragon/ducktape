import pytest_bazel
from google.protobuf import descriptor_pb2, struct_pb2

from util.protobuf import message_dict


def test_message_dict_preserves_schema_names_and_field_presence() -> None:
    message = descriptor_pb2.FieldDescriptorProto(name="test_field", number=0, type_name=".test.Entry")

    assert message_dict(message) == {"name": "test_field", "number": 0, "type_name": ".test.Entry"}


def test_message_dict_sorts_map_keys_without_changing_them() -> None:
    message = struct_pb2.Struct(
        fields={
            "z.route": struct_pb2.Value(string_value="last"),
            "A.Route": struct_pb2.Value(string_value="upper"),
            "a.route": struct_pb2.Value(string_value="lower"),
        }
    )

    rendered = message_dict(message)

    assert rendered == {"A.Route": "upper", "a.route": "lower", "z.route": "last"}
    assert list(rendered) == ["A.Route", "a.route", "z.route"]


if __name__ == "__main__":
    pytest_bazel.main()
