"""Protobuf messages stored as wire bytes, without a JSON or enum-name round trip."""

from typing import override

from google.protobuf.message import Message
from sqlalchemy import LargeBinary
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class ProtobufColumn[M: Message](TypeDecorator[M]):
    """Assign a new message to change a value; in-place protobuf mutation is not tracked.

    Readers retain unknown fields and numeric enum values for later writers. Understanding an
    operation is still the admission boundary's responsibility, not a storage-type decision.
    """

    impl = LargeBinary
    cache_ok = True

    def __init__(self, message_type: type[M]) -> None:
        super().__init__()
        self.message_type = message_type

    @override
    def process_bind_param(self, value: M | None, dialect: Dialect) -> bytes | None:
        if value is None:
            return None
        if not isinstance(value, self.message_type):
            raise TypeError(f"expected {self.message_type.__name__}")
        return value.SerializeToString(deterministic=True)

    @override
    def process_result_value(self, value: bytes | None, dialect: Dialect) -> M | None:
        if value is None:
            return None
        result = self.message_type()
        result.ParseFromString(value)
        return result
