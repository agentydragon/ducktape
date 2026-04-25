"""Post-merge constraint checks for the casino's Y.Doc.

CRDTs (the way pycrdt/Yjs implement them) guarantee convergence but
have no concept of business rules — concurrent decrements on `credits`
will happily converge to a negative value if both happened
independently. The casino's economy needs `credits ≥ 0`, `tokens ≥ 0`,
prize redemption only when affordable, and so on. Those rules are not
representable as commutative ops; they are *integrity constraints*.

The strategy: the server's `/sync` handler accepts a client update
into a *trial* Doc, runs every validator below against that trial,
and either promotes it to canonical (rules pass) or rejects it (rules
fail). On rejection the client gets a structured error and undoes its
last local transaction via `Y.UndoManager`.

A consequence: if two devices each spend the same credits while
offline and both validate locally, both will be rejected by the
*second* sync attempt — but the second device will see the rejection
and roll back, so credits never go negative. The single-user app's
risk of two truly simultaneous spends is low; we accept it as a
documented limitation rather than serializing every action through a
distributed lock.

Each validator raises `ValidationError` on failure. The /sync handler
catches that and translates to a 409 with the exception message and
the violated rule name attached.
"""

from __future__ import annotations

from dataclasses import dataclass

from x.auragon_study_casino.doc_shape import Casino


@dataclass(frozen=True)
class ValidationError(Exception):
    """Constraint violation surfaced to the client as a sync rejection."""

    rule: str
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.message}"


def validate_credits_nonneg(casino: Casino) -> None:
    credits = int(casino.balance.get("credits", 0))
    if credits < 0:
        raise ValidationError(rule="credits_nonneg", message=f"credits would land at {credits}; must be ≥ 0")


def validate_tokens_nonneg(casino: Casino) -> None:
    tokens = int(casino.balance.get("tokens", 0))
    if tokens < 0:
        raise ValidationError(rule="tokens_nonneg", message=f"tokens would land at {tokens}; must be ≥ 0")


def validate_prize_catalog_shape(casino: Casino) -> None:
    """Each prize must have a non-empty name and a positive cost.

    pycrdt stores numbers as JS-style float64, so we accept either int or
    float and only check positivity — the UI rounds for display.
    """
    for prize_id, prize in casino.prizes.items():
        name = prize.get("name")
        cost = prize.get("cost")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(rule="prize_name", message=f"prize {prize_id!r} has empty/non-string name")
        if not isinstance(cost, (int, float)) or cost <= 0:
            raise ValidationError(
                rule="prize_cost", message=f"prize {prize_id!r} cost {cost!r} must be a positive number"
            )


def validate_session_shape(casino: Casino) -> None:
    """Completed sessions must have subject (str), seconds (≥0), ended_at_ms (number)."""
    for sid, session in casino.sessions.items():
        subject = session.get("subject")
        seconds = session.get("seconds")
        ended_at = session.get("ended_at_ms")
        if not isinstance(subject, str) or not subject:
            raise ValidationError(
                rule="session_subject", message=f"session {sid!r} subject {subject!r} not a non-empty string"
            )
        if not isinstance(seconds, (int, float)) or seconds < 0:
            raise ValidationError(
                rule="session_seconds", message=f"session {sid!r} seconds {seconds!r} not a non-negative number"
            )
        if not isinstance(ended_at, (int, float)):
            raise ValidationError(
                rule="session_ended_at", message=f"session {sid!r} ended_at_ms {ended_at!r} not a number"
            )


ALL_VALIDATORS = (validate_credits_nonneg, validate_tokens_nonneg, validate_prize_catalog_shape, validate_session_shape)


def validate(casino: Casino) -> None:
    """Run every validator against `casino`, raising the first violation.

    The /sync handler calls this on a trial Casino built by applying the
    inbound client update on top of the canonical Doc. The first failing
    rule short-circuits — the client only needs one error to act on.
    """
    for v in ALL_VALIDATORS:
        v(casino)
