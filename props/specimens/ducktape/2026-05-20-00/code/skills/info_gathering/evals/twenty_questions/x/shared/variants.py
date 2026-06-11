"""Twenty Questions game variant definitions."""

from dataclasses import dataclass


@dataclass
class Variant:
    domain_description: str
    secret: str
    turn_limit: int = 20


VARIANTS: dict[str, Variant] = {
    "states": Variant(domain_description="a US state", secret="New Mexico"),
    "wide": Variant(
        domain_description="a thing — could be anything: object, place, concept, activity, anything",
        secret="a sourdough starter",
        turn_limit=40,
    ),
}
