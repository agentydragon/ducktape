"""A test's responses driving an `ActionSession` from its start to its finish."""

from collections.abc import Callable

from finance.augur.sim.actions import Action, DecisionActions
from finance.augur.sim.observations import Decision, Observation
from finance.augur.sim.results import Finished
from finance.augur.sim.session import ActionSession

type Respond = Callable[[list[Decision]], list[DecisionActions]]


def finish(session: ActionSession, respond: Respond) -> Finished:
    """Answer every batch with `respond` until each path finishes; the session is closed however this ends."""
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(respond(batch))
        return batch
    finally:
        session.close()


def each(decide: Callable[[Observation], list[Action]]) -> Respond:
    """Answer each path's decision from its own observation alone."""
    return lambda batch: [
        DecisionActions(decision.rollout_id, decision.observation.month, decide(decision.observation))
        for decision in batch
    ]
