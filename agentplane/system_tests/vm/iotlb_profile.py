"""Build-scoped register recipes for the launcher's downstream QEMU binary."""

import hashlib

from pydantic import BaseModel, ConfigDict

from agentplane.system_tests.vm import qemu_elf


class Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    address: int
    offset: int
    instruction: str
    arguments: dict[str, str]


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_id: str
    supplementary_build_id: str
    binary_sha256: str
    probes: list[Probe]

    def verify(self, binary: bytes) -> None:
        if qemu_elf.build_id(binary) != self.build_id or hashlib.sha256(binary).hexdigest() != self.binary_sha256:
            raise ValueError("QEMU executable identity mismatch")
        for probe in self.probes:
            offset = qemu_elf.text_offset(binary, probe.address)
            instruction = bytes.fromhex(probe.instruction)
            if offset != probe.offset or binary[offset : offset + len(instruction)] != instruction:
                raise ValueError(f"Instruction bytes mismatch at {probe.name}")
