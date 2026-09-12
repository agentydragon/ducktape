"""The one shape every tracked thing has: typed messages in, typed messages out."""

from abc import ABC, abstractmethod

from finance.augur.sim.books import Record


class Statement(Record):
    """What an emitter tells an addressee when a month opens; needs no reply."""

    month: int


class MonthOpened(Record):
    """Delivered after an actor's statements and dues; the message that makes it act."""

    month: int


class Actor[In, Out](ABC):
    """Receives the messages addressed to it and returns the messages it emits, in order.

    The generic parameters say what an actor accepts and produces; `handle` bodies are
    checked against them. A statement needs no reply, so handling one returns nothing.
    Mail is delivered to the object itself; an actor the ledger scopes carries the
    `AgentId` its accounts are declared under.
    """

    @abstractmethod
    def handle(self, message: In) -> list[Out]: ...
