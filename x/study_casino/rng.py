"""Auditable deterministic randomness for server-resolved casino games."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True)
class RngCallAudit:
    call_index: int
    purpose: str
    method: str
    parameters: dict[str, Any]
    result: dict[str, Any]


@dataclass(frozen=True)
class RngActionAudit:
    rng_version: str
    rng_key_id: str
    seed_material_json: str
    seed_digest_hex: str
    calls: tuple[RngCallAudit, ...]


class AuditedRandom:
    """Deterministic HMAC-SHA256 random source that records every draw."""

    def __init__(
        self,
        *,
        secret: bytes,
        rng_version: str,
        rng_key_id: str,
        seed_material: dict[str, Any] | None = None,
        seed_material_json: str | None = None,
    ) -> None:
        if (seed_material is None) == (seed_material_json is None):
            raise ValueError("provide exactly one of seed_material or seed_material_json")
        self.rng_version = rng_version
        self.rng_key_id = rng_key_id
        self.seed_material_json = (
            seed_material_json if seed_material_json is not None else canonical_json(seed_material)
        )
        self.seed_digest_hex = hmac.new(secret, self.seed_material_json.encode(), hashlib.sha256).hexdigest()
        self._action_key = bytes.fromhex(self.seed_digest_hex)
        self._calls: list[RngCallAudit] = []

    @classmethod
    def for_action(
        cls,
        *,
        secret: bytes,
        rng_version: str,
        rng_key_id: str,
        user_id: str,
        client_action_id: str,
        action_type: str,
        request_body: dict[str, Any],
    ) -> AuditedRandom:
        return cls(
            secret=secret,
            rng_version=rng_version,
            rng_key_id=rng_key_id,
            seed_material={
                "rng_version": rng_version,
                "rng_key_id": rng_key_id,
                "user_id": user_id,
                "client_action_id": client_action_id,
                "action_type": action_type,
                "request_body": request_body,
            },
        )

    @classmethod
    def from_seed_material_json(
        cls, *, secret: bytes, rng_version: str, rng_key_id: str, seed_material_json: str
    ) -> AuditedRandom:
        return cls(secret=secret, rng_version=rng_version, rng_key_id=rng_key_id, seed_material_json=seed_material_json)

    def audit(self) -> RngActionAudit:
        return RngActionAudit(
            rng_version=self.rng_version,
            rng_key_id=self.rng_key_id,
            seed_material_json=self.seed_material_json,
            seed_digest_hex=self.seed_digest_hex,
            calls=tuple(self._calls),
        )

    def randbelow(self, upper: int, *, purpose: str, parameters: dict[str, Any] | None = None) -> int:
        value, call_parameters, rejections = self._draw_below(
            upper=upper, purpose=purpose, method="randbelow", parameters=parameters
        )
        self._record(
            purpose=purpose,
            method="randbelow",
            parameters=call_parameters,
            result={"value": value, "rejections": rejections},
        )
        return value

    def weighted_choice(
        self,
        items: list[dict[str, Any]],
        *,
        weights: list[int],
        purpose: str,
        item_id_key: str,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(items) != len(weights):
            raise ValueError("items and weights must have the same length")
        int_weights = [int(weight) for weight in weights]
        if any(weight <= 0 for weight in int_weights):
            raise ValueError("weights must be positive")

        item_ids = [str(item[item_id_key]) for item in items]
        total = sum(int_weights)
        draw_parameters = {**(parameters or {}), "items": item_ids, "weights": int_weights, "total_weight": total}
        draw, call_parameters, rejections = self._draw_below(
            upper=total, purpose=purpose, method="weighted_choice", parameters=draw_parameters
        )

        remaining = draw
        for index, weight in enumerate(int_weights):
            remaining -= weight
            if remaining < 0:
                self._record(
                    purpose=purpose,
                    method="weighted_choice",
                    parameters=call_parameters,
                    result={"draw": draw, "index": index, "item_id": item_ids[index], "rejections": rejections},
                )
                return items[index]
        raise RuntimeError("weighted choice fell through despite valid weights")

    def shuffle(self, items: list[Any], *, purpose: str, parameters: dict[str, Any] | None = None) -> None:
        length = len(items)
        base_parameters = parameters or {}
        for i in range(length - 1, 0, -1):
            call_parameters = {**base_parameters, "length": length, "i": i}
            j, recorded_parameters, rejections = self._draw_below(
                upper=i + 1, purpose=purpose, method="shuffle_swap", parameters=call_parameters
            )
            items[i], items[j] = items[j], items[i]
            self._record(
                purpose=purpose,
                method="shuffle_swap",
                parameters=recorded_parameters,
                result={"j": j, "rejections": rejections},
            )

    def _draw_below(
        self, *, upper: int, purpose: str, method: str, parameters: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any], int]:
        if upper <= 0:
            raise ValueError("upper must be positive")
        if upper >= 2**256:
            raise ValueError("upper must fit within the HMAC digest size")

        call_index = len(self._calls)
        call_parameters = {**(parameters or {}), "upper": upper}
        nbytes = max(1, (upper.bit_length() + 7) // 8)
        space = 1 << (8 * nbytes)
        limit = space - (space % upper)
        block = 0

        while True:
            block_material = canonical_json(
                {
                    "call_index": call_index,
                    "purpose": purpose,
                    "method": method,
                    "parameters": call_parameters,
                    "block": block,
                }
            )
            digest = hmac.new(self._action_key, block_material.encode(), hashlib.sha256).digest()
            candidate = int.from_bytes(digest[:nbytes], "big")
            if candidate < limit:
                return candidate % upper, call_parameters, block
            block += 1

    def _record(self, *, purpose: str, method: str, parameters: dict[str, Any], result: dict[str, Any]) -> None:
        self._calls.append(
            RngCallAudit(
                call_index=len(self._calls), purpose=purpose, method=method, parameters=parameters, result=result
            )
        )


@dataclass(frozen=True)
class ActionRngFactory:
    secret: bytes
    rng_version: str
    rng_key_id: str

    def for_action(
        self, *, user_id: str, client_action_id: str, action_type: str, request_body: dict[str, Any]
    ) -> AuditedRandom:
        return AuditedRandom.for_action(
            secret=self.secret,
            rng_version=self.rng_version,
            rng_key_id=self.rng_key_id,
            user_id=user_id,
            client_action_id=client_action_id,
            action_type=action_type,
            request_body=request_body,
        )
