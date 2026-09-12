"""Python-owned monthly orchestration with one ordered batch policy response per month.

The batch session drives one `World` per selected path through the same open, act
and close steps a standalone world runs for a tracked agent; only the caller's
policy sees every path at once.
"""

from finance.augur.sim import results
from finance.augur.sim.actions import Action, DecisionActions
from finance.augur.sim.observations import Decision, Observation
from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.validation import validate
from finance.augur.sim.world import Capture, World, validate_actor


class _Session:
    """Own the selected worlds, the shared clock and the batch routing envelope."""

    def __init__(
        self,
        run: CompiledRun,
        actor: str | None,
        rollout_ids: list[int],
        *,
        capture: Capture,
        configured: bool = False,
        product_actor: str | None = None,
    ) -> None:
        if not isinstance(run, CompiledRun):
            raise TypeError("execution requires a CompiledRun, not serialized input")
        if (
            not rollout_ids
            or len(set(rollout_ids)) != len(rollout_ids)
            or any(
                not isinstance(id_, int) or isinstance(id_, bool) or not 0 <= id_ < run.rollout_count
                for id_ in rollout_ids
            )
        ):
            raise ValueError("selected rollout IDs must be unique, nonempty and in range")
        if capture not in ("summary", "dense", "forensic"):
            raise ValueError("capture must be summary, dense or forensic")
        if not configured:
            if actor is None:
                raise ValueError("action sessions require an actor")
            validate_actor(run, actor)
        validate(run)
        self.run = run
        self.actor = actor
        self.configured = configured
        self.capture = capture
        self.month = 0
        self.started = False
        self.closed = False
        self.paths = {
            rollout_id: World(
                run, rollout_id, capture_mode=capture, actor=None if configured else actor, product_actor=product_actor
            )
            for rollout_id in rollout_ids
        }

    def active(self) -> dict[int, World]:
        return {id_: path for id_, path in self.paths.items() if path.result is None}

    def is_finished(self) -> bool:
        return self.started and all(path.result is not None for path in self.paths.values())

    def _check_open(self) -> None:
        if self.closed or self.is_finished():
            raise ValueError("session is finished, aborted or closed")

    def start(self) -> None:
        self._check_open()
        if self.started:
            raise ValueError("invalid session lifecycle state: already started")
        self.started = True
        for path in self.active().values():
            path.start()

    def observe(self, rollout_id: int, actor: str) -> Observation:
        return self.paths[rollout_id].observe(actor)

    def begin_actions(self, responses: list[DecisionActions]) -> None:
        """Validate the complete routing envelope before executing any action."""
        self._check_open()
        if not self.started:
            raise ValueError("invalid session lifecycle state: not started")
        if not all(isinstance(response, DecisionActions) for response in responses):
            raise TypeError("responses must be DecisionActions")
        if any(
            not isinstance(key, int) or isinstance(key, bool)
            for response in responses
            for key in (response.rollout_id, response.month)
        ):
            raise TypeError("response rollout ID and month must be integers")
        keys = [(response.rollout_id, response.month) for response in responses]
        if len(set(keys)) != len(keys) or set(keys) != {(id_, self.month) for id_ in self.active()}:
            raise ValueError("responses must name each active path/month exactly once")
        for response in responses:
            self.paths[response.rollout_id].check_claims(response.actions)
        for response in responses:
            self.paths[response.rollout_id].begin_actions(response.actions)

    def apply(self, rollout_id: int, action: Action) -> results.Receipt:
        return self.paths[rollout_id].execute(action)

    def close_month(self) -> None:
        for path in self.active().values():
            path.close_month()
        self.month += 1

    def close(self) -> None:
        self.closed = True
        self.paths.clear()


class ActionSession:
    """One household's batch session; only the caller invokes policy code.

    Submit one keyed ordered response per current path. Rejection stops that path,
    preserving earlier effects; no retries or engine-selected rescue actions occur.
    """

    def __init__(self, run: CompiledRun, actor: str, rollout_ids: list[int], *, capture: Capture = "forensic") -> None:
        self._session = _Session(run, actor, rollout_ids, capture=capture)

    def _result(self) -> list[Decision] | results.Finished:
        session = self._session
        if session.is_finished():
            return results.Finished(rollouts=[path.rollout() for path in session.paths.values()])
        if session.actor is None:
            raise RuntimeError("action sessions require an actor")
        return [Decision(id_, session.observe(id_, session.actor)) for id_ in session.active()]

    def start(self) -> list[Decision] | results.Finished:
        try:
            self._session.start()
            return self._result()
        except BaseException:
            self.close()
            raise

    def advance(self, responses: list[DecisionActions]) -> list[Decision] | results.Finished:
        try:
            self._session.begin_actions(responses)
            for response in responses:
                for action in response.actions:
                    receipt = self._session.apply(response.rollout_id, action)
                    if isinstance(receipt.outcome, results.Rejected):
                        break
            self._session.close_month()
            return self._result()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._session.close()
