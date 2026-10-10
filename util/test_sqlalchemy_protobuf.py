"""Wire compatibility and typed reads through actual SQLAlchemy bind/result processing."""

import pytest_bazel
from google.protobuf.struct_pb2 import Value
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select

from util.sqlalchemy_protobuf import ProtobufColumn


def test_round_trip_preserves_unknown_enum_numbers_and_fields() -> None:
    # null_value enum = 123 (unknown today), plus unknown varint field 100 = 123.
    message = Value.FromString(b"\x08\x7b\xa0\x06\x7b")
    assert message.null_value == 123
    engine = create_engine("sqlite://")
    metadata = MetaData()
    rows = Table(
        "messages", metadata, Column("id", Integer, primary_key=True), Column("payload", ProtobufColumn(Value))
    )
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(rows.insert().values(id=1, payload=message))
            restored = connection.scalar(select(rows.c.payload).where(rows.c.id == 1))
            assert isinstance(restored, Value)
            assert restored == message
            assert restored.null_value == 123
            assert restored.SerializeToString(deterministic=True) == message.SerializeToString(deterministic=True)
            connection.execute(rows.insert().values(id=2, payload=restored))
            assert connection.scalar(select(rows.c.payload).where(rows.c.id == 2)) == message
    finally:
        engine.dispose()


def test_null_and_fresh_message_instances() -> None:
    engine = create_engine("sqlite://")
    metadata = MetaData()
    rows = Table(
        "messages", metadata, Column("id", Integer, primary_key=True), Column("payload", ProtobufColumn(Value))
    )
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                rows.insert(), [{"id": 1, "payload": None}, {"id": 2, "payload": Value(string_value="saved")}]
            )
            assert connection.scalar(select(rows.c.payload).where(rows.c.id == 1)) is None
            first = connection.scalar(select(rows.c.payload).where(rows.c.id == 2))
            assert isinstance(first, Value)
            first.string_value = "local mutation"
            second = connection.scalar(select(rows.c.payload).where(rows.c.id == 2))
            assert isinstance(second, Value)
            assert second.string_value == "saved"
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
